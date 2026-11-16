#include "http/websocket_session.hpp"

#include <atomic>
#include <deque>
#include <mutex>
#include <optional>

#include <boost/asio/io_context.hpp>
#include <boost/asio/post.hpp>
#include <boost/beast/core/buffers_to_string.hpp>
#include <boost/beast/core/flat_buffer.hpp>
#include <boost/beast/core/tcp_stream.hpp>
#include <boost/beast/websocket.hpp>

#include "util/log.hpp"
#include "util/text.hpp"

namespace gw::http {

namespace net = boost::asio;
namespace beast = boost::beast;
namespace bhttp = boost::beast::http;
namespace websocket = boost::beast::websocket;

using tcp = net::ip::tcp;

namespace {

//
// The accepted socket belongs to the acceptor's event loop, which
// nobody runs; a socket for async use must belong to the loop that
// runs it, so its descriptor moves to a new socket there.
//
tcp::socket rehome(
    net::io_context& context,
    tcp::socket& socket
) {
    const auto protocol = socket.local_endpoint().protocol();

    tcp::socket moved(context);

    moved.assign(protocol, socket.release());

    return moved;
}

class Session : public WebSocket, public std::enable_shared_from_this<Session> {
public:
    Session(
        tcp::socket& socket,
        const WebSocketOptions& options
    )
        : stream_(rehome(context_, socket)),
          options_(options) {}

    bool send(
        std::string text
    ) override {
        //
        // Posting only while open, under the lock that run() takes to
        // close for good, so nothing is posted to a finished loop
        // (where it would keep the session alive forever).
        //
        const std::lock_guard lock(post_mutex_);

        if (!open_.load()) {
            return false;
        }

        if (queued_.load() >= options_.max_queued_messages) {
            //
            // The client does not keep up; drop it rather than let
            // the queue grow without bound.
            //
            log::warning("WebSocket client too slow, closing the connection");

            open_ = false;

            net::post(context_, [self = shared_from_this()]() { self->close_now(); });

            return false;
        }

        ++queued_;

        net::post(
            context_,
            [self = shared_from_this(), text = std::move(text)]() mutable {
                self->queue_.push_back(std::move(text));

                if (self->accepted_ && self->queue_.size() == 1) {
                    self->write_next();
                }
            }
        );

        return true;
    }

    //
    // Handshake, then messages both ways until the connection ends;
    // returns then.
    //
    void run(
        const bhttp::request<bhttp::string_body>& upgrade,
        WebSocketCallbacks callbacks
    ) {
        callbacks_ = std::move(callbacks);

        auto timeouts = websocket::stream_base::timeout::suggested(beast::role_type::server);

        timeouts.idle_timeout = std::chrono::seconds(options_.idle_timeout_seconds);
        timeouts.keep_alive_pings = true;

        stream_.set_option(timeouts);
        stream_.read_message_max(options_.max_message_bytes);
        stream_.text(true);

        stream_.async_accept(
            upgrade,
            [self = shared_from_this()](beast::error_code error) {
                if (error) {
                    self->close_now();
                    return;
                }

                self->accepted_ = true;

                if (!self->queue_.empty()) {
                    self->write_next();
                }

                self->read_next();
            }
        );

        context_.run();

        {
            const std::lock_guard lock(post_mutex_);

            open_ = false;
        }

        //
        // Run what was posted before closing (writes now fail at
        // once), so those handlers release the session.
        //
        close_now();

        context_.restart();

        while (context_.poll() > 0) {
        }

        if (callbacks_.on_close) {
            callbacks_.on_close();
        }

        //
        // The callbacks may hold references back to this session.
        //
        callbacks_ = {};
        queue_.clear();
    }

    //
    // Instead of the handshake: HTTP 403 and close.
    //
    void refuse(
        unsigned version
    ) {
        open_ = false;

        bhttp::response<bhttp::string_body> response{bhttp::status::forbidden, version};

        response.set(bhttp::field::content_type, "text/plain; charset=utf-8");
        response.body() = "Forbidden";
        response.prepare_payload();
        response.keep_alive(false);

        //
        // Synchronous writes work whether or not the loop runs.
        //
        auto& socket = beast::get_lowest_layer(stream_).socket();

        beast::error_code ignored;

        bhttp::write(socket, response, ignored);

        socket.shutdown(tcp::socket::shutdown_send, ignored);
        socket.close(ignored);
    }

private:
    void read_next() {
        stream_.async_read(
            buffer_,
            [self = shared_from_this()](beast::error_code error, std::size_t) {
                if (error) {
                    //
                    // Closed by the client, idle timeout, or a
                    // protocol error: the session ends.
                    //
                    self->close_now();
                    return;
                }

                const std::string text = beast::buffers_to_string(self->buffer_.data());

                self->buffer_.consume(self->buffer_.size());

                if (self->callbacks_.on_message) {
                    try {
                        self->callbacks_.on_message(text);
                    } catch (const std::exception& exception) {
                        log::error(format("WebSocket message handler failed: %s", exception.what()));
                    }
                }

                self->read_next();
            }
        );
    }

    void write_next() {
        stream_.async_write(
            net::buffer(queue_.front()),
            [self = shared_from_this()](beast::error_code error, std::size_t) {
                self->queue_.pop_front();
                self->queued_.fetch_sub(1);

                if (error) {
                    self->close_now();
                    return;
                }

                if (!self->queue_.empty()) {
                    self->write_next();
                }
            }
        );
    }

    //
    // On the session's own thread: closing the socket cancels the
    // pending read and write, after which run() returns.
    //
    void close_now() {
        open_ = false;

        auto& socket = beast::get_lowest_layer(stream_).socket();

        if (socket.is_open()) {
            beast::error_code ignored;

            socket.shutdown(tcp::socket::shutdown_both, ignored);
            socket.close(ignored);
        }
    }

    //
    // First, so it exists when stream_ is built on it.
    //
    net::io_context context_{1};

    websocket::stream<beast::tcp_stream> stream_;

    const WebSocketOptions& options_;

    WebSocketCallbacks callbacks_;

    beast::flat_buffer buffer_;

    //
    // Only touched on the session's thread.
    //
    std::deque<std::string> queue_;
    bool accepted_ = false;

    std::mutex post_mutex_;

    std::atomic<bool> open_{true};
    std::atomic<std::size_t> queued_{0};
};

}  // namespace

void run_websocket(
    tcp::socket socket,
    bhttp::request<bhttp::string_body> upgrade,
    Request request,
    const WebSocketHandler& handler,
    const WebSocketOptions& options
) {
    //
    // The session exists before the handler decides, since the
    // handler needs its sending side (to register it for events).
    //
    auto session = std::make_shared<Session>(socket, options);

    std::optional<WebSocketCallbacks> callbacks;

    try {
        callbacks = handler(request, session);
    } catch (const std::exception& exception) {
        log::error(format("WebSocket handler failed: %s", exception.what()));
    }

    if (!callbacks) {
        session->refuse(upgrade.version());

        return;
    }

    session->run(upgrade, std::move(*callbacks));
}

}  // namespace gw::http
