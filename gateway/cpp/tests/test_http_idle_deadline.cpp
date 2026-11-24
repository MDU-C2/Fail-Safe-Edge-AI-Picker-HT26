#include <chrono>
#include <future>
#include <string>
#include <thread>
#include <sys/socket.h>
#include <sys/time.h>

#include <boost/asio/buffer.hpp>
#include <boost/asio/write.hpp>
#include <boost/beast/core/flat_buffer.hpp>
#include <boost/beast/http.hpp>
#include <gtest/gtest.h>

#include "http/idle_deadline_stream.hpp"

namespace {

namespace net = boost::asio;
namespace bhttp = boost::beast::http;
using tcp = net::ip::tcp;
using namespace std::chrono_literals;

struct ConnectedSockets {
    net::io_context context;
    tcp::acceptor listener{context, {net::ip::address_v4::loopback(), 0}};
    tcp::socket peer{context};
    tcp::socket server{context};

    ConnectedSockets() {
        peer.connect(listener.local_endpoint());
        listener.accept(server);
    }

    void close_peer() {
        boost::system::error_code ignored;
        peer.close(ignored);
    }
};

// Reproduce why the former SO_RCVTIMEO guard could retain disconnected
// clients forever: Asio retries the kernel timeout with an unbounded poll.
TEST(HttpIdleDeadline, NativeReceiveTimeoutDoesNotBoundBlockingAsioRead) {
    ConnectedSockets sockets;
    timeval timeout{};
    timeout.tv_usec = 50000;
    ASSERT_EQ(::setsockopt(sockets.server.native_handle(), SOL_SOCKET, SO_RCVTIMEO,
                          &timeout, sizeof(timeout)), 0);
    auto result = std::async(std::launch::async, [&] {
        char byte;
        boost::system::error_code error;
        sockets.server.read_some(net::buffer(&byte, 1), error);
        return error;
    });
    EXPECT_EQ(result.wait_for(250ms), std::future_status::timeout);
    sockets.close_peer();
    EXPECT_EQ(result.get(), net::error::eof);
}

TEST(HttpIdleDeadline, IdleRequestTimesOutAndReleasesItsSocket) {
    ConnectedSockets sockets;
    auto result = std::async(std::launch::async, [&] {
        tcp::socket owned(std::move(sockets.server));
        gw::http::IdleDeadlineStream stream(owned, 60ms);
        boost::beast::flat_buffer buffer;
        bhttp::request<bhttp::string_body> request;
        boost::system::error_code error;
        bhttp::read(stream, buffer, request, error);
        return error;
    });
    const auto readiness = result.wait_for(1s);
    if (readiness != std::future_status::ready) {
        sockets.close_peer();
    }
    ASSERT_EQ(readiness, std::future_status::ready);
    EXPECT_EQ(result.get(), net::error::timed_out);
    char byte;
    boost::system::error_code error;
    sockets.peer.read_some(net::buffer(&byte, 1), error);
    EXPECT_EQ(error, net::error::eof);
}

TEST(HttpIdleDeadline, PartialRequestHeaderTimesOut) {
    ConnectedSockets sockets;
    net::write(sockets.peer, net::buffer(std::string("GET /health HTTP/1.1\r\nHost:")));
    gw::http::IdleDeadlineStream stream(sockets.server, 60ms);
    boost::beast::flat_buffer buffer;
    bhttp::request<bhttp::string_body> request;
    boost::system::error_code error;
    bhttp::read(stream, buffer, request, error);
    EXPECT_EQ(error, net::error::timed_out);
}

TEST(HttpIdleDeadline, PartialRequestBodyTimesOut) {
    ConnectedSockets sockets;
    net::write(sockets.peer, net::buffer(std::string(
        "POST /sample HTTP/1.1\r\nHost: localhost\r\nContent-Length: 4\r\n\r\nx")));
    gw::http::IdleDeadlineStream stream(sockets.server, 60ms);
    boost::beast::flat_buffer buffer;
    bhttp::request<bhttp::string_body> request;
    boost::system::error_code error;
    bhttp::read(stream, buffer, request, error);
    EXPECT_EQ(error, net::error::timed_out);
}

TEST(HttpIdleDeadline, ProgressingFragmentedBodyMayExceedOneIdleInterval) {
    ConnectedSockets sockets;
    auto result = std::async(std::launch::async, [&] {
        gw::http::IdleDeadlineStream stream(sockets.server, 150ms);
        boost::beast::flat_buffer buffer;
        bhttp::request<bhttp::string_body> request;
        bhttp::read(stream, buffer, request);
        return request.body();
    });
    net::write(sockets.peer, net::buffer(std::string(
        "POST /sample HTTP/1.1\r\nHost: localhost\r\nContent-Length: 5\r\n\r\n")));
    for (char letter : std::string("hello")) {
        std::this_thread::sleep_for(50ms);
        net::write(sockets.peer, net::buffer(&letter, 1));
    }
    EXPECT_EQ(result.get(), "hello");
}

TEST(HttpIdleDeadline, SlowHandlerIsNotInterruptedAndConnectionCloseIsPreserved) {
    ConnectedSockets sockets;
    auto result = std::async(std::launch::async, [&] {
        tcp::socket owned(std::move(sockets.server));
        gw::http::IdleDeadlineStream stream(owned, 60ms);
        boost::beast::flat_buffer buffer;
        bhttp::request<bhttp::string_body> request;
        bhttp::read(stream, buffer, request);
        std::this_thread::sleep_for(180ms);
        bhttp::response<bhttp::string_body> response{bhttp::status::ok, request.version()};
        response.body() = "completed";
        response.keep_alive(request.keep_alive());
        response.prepare_payload();
        bhttp::write(stream, response);
    });
    net::write(sockets.peer, net::buffer(std::string(
        "GET /sample HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n")));
    boost::beast::flat_buffer buffer;
    bhttp::response<bhttp::string_body> response;
    bhttp::read(sockets.peer, buffer, response);
    EXPECT_EQ(response.body(), "completed");
    EXPECT_FALSE(response.keep_alive());
    result.get();
    char byte;
    boost::system::error_code error;
    sockets.peer.read_some(net::buffer(&byte, 1), error);
    EXPECT_EQ(error, net::error::eof);
}

TEST(HttpIdleDeadline, NonReadingPeerDoesNotHoldALargeResponseForever) {
    ConnectedSockets sockets;
    sockets.server.set_option(net::socket_base::send_buffer_size(1024));
    sockets.peer.set_option(net::socket_base::receive_buffer_size(1024));
    auto result = std::async(std::launch::async, [&] {
        gw::http::IdleDeadlineStream stream(sockets.server, 60ms);
        const std::string payload(8 * 1024 * 1024, 'x');
        boost::system::error_code error;
        net::write(stream, net::buffer(payload), error);
        return error;
    });
    const auto readiness = result.wait_for(2s);
    if (readiness != std::future_status::ready) {
        sockets.close_peer();
    }
    ASSERT_EQ(readiness, std::future_status::ready);
    EXPECT_EQ(result.get(), net::error::timed_out);
}

TEST(HttpIdleDeadline, WebSocketHandoffRestoresExistingBlockingMode) {
    ConnectedSockets sockets;
    gw::http::IdleDeadlineStream stream(sockets.server, 60ms);
    EXPECT_TRUE(sockets.server.non_blocking());
    stream.restore_blocking();
    EXPECT_FALSE(sockets.server.non_blocking());
}

}  // namespace
