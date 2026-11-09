#include "http/server.hpp"

#include <sys/socket.h>
#include <sys/time.h>

#include <boost/asio/buffer.hpp>
#include <boost/asio/ip/tcp.hpp>
#include <boost/asio/write.hpp>
#include <boost/beast/core/flat_buffer.hpp>
#include <boost/beast/http.hpp>

#include "util/log.hpp"
#include "util/text.hpp"
#include "util/thread.hpp"

namespace gw::http {

namespace net = boost::asio;
namespace beast = boost::beast;
namespace bhttp = boost::beast::http;

using tcp = net::ip::tcp;

namespace {

// ================================================================
// Requests
// ================================================================

void parse_target(
    std::string_view target,
    Request& request
) {
    const auto question = target.find('?');

    request.path = std::string(target.substr(0, question));

    if (question == std::string_view::npos) {
        return;
    }

    std::string_view query = target.substr(question + 1);

    while (!query.empty()) {
        const auto amp = query.find('&');
        const std::string_view pair = query.substr(0, amp);

        if (!pair.empty()) {
            const auto equals = pair.find('=');

            const std::string key = url_decode(pair.substr(0, equals), true);
            const std::string value = equals == std::string_view::npos
                ? std::string()
                : url_decode(pair.substr(equals + 1), true);

            //
            // A repeated parameter keeps its last value.
            //
            request.query[key] = value;
        }

        if (amp == std::string_view::npos) {
            break;
        }

        query.remove_prefix(amp + 1);
    }
}

// ================================================================
// Responses
// ================================================================

bool write_response(
    tcp::socket& socket,
    const Response& response,
    unsigned version,
    bool keep_alive
) {
    bhttp::response<bhttp::empty_body> header{
        static_cast<bhttp::status>(response.status),
        version,
    };

    header.set(bhttp::field::content_type, response.content_type);

    for (const auto& [name, value] : response.headers) {
        header.set(name, value);
    }

    header.content_length(response.body_size());
    header.keep_alive(keep_alive);

    beast::error_code error;

    bhttp::response_serializer<bhttp::empty_body> serializer{header};

    bhttp::write_header(socket, serializer, error);

    if (error) {
        return false;
    }

    if (response.body_size() > 0) {
        net::write(
            socket,
            net::buffer(response.body_data(), response.body_size()),
            error
        );
    }

    return !error;
}

void write_error(
    tcp::socket& socket,
    int status,
    const char* detail
) {
    write_response(socket, error_response(status, detail), 11, false);
}

// ================================================================
// Connections
// ================================================================

void set_receive_timeout(
    tcp::socket& socket,
    int seconds
) {
    timeval timeout{};

    timeout.tv_sec = seconds;

    setsockopt(socket.native_handle(), SOL_SOCKET, SO_RCVTIMEO, &timeout, sizeof(timeout));
}

void serve_connection(
    tcp::socket socket,
    const Router& router,
    const ServerOptions& options
) {
    set_receive_timeout(socket, options.keep_alive_seconds);

    beast::flat_buffer buffer;

    while (true) {
        beast::error_code error;

        bhttp::request_parser<bhttp::string_body> parser;

        parser.body_limit(options.max_body_bytes);
        parser.header_limit(64 * 1024);

        bhttp::read_header(socket, buffer, parser, error);

        if (error == bhttp::error::header_limit) {
            write_error(socket, 431, "Request Header Fields Too Large");
            return;
        }

        if (error) {
            //
            // Closed by the client, idle timeout or garbage.
            //
            return;
        }

        const auto& header = parser.get();

        if (bhttp::token_list(header[bhttp::field::expect]).exists("100-continue")) {
            bhttp::response<bhttp::empty_body> go_on{bhttp::status::continue_, header.version()};

            bhttp::write(socket, go_on, error);

            if (error) {
                return;
            }
        }

        bhttp::read(socket, buffer, parser, error);

        if (error == bhttp::error::body_limit) {
            write_error(socket, 413, "Request Entity Too Large");
            return;
        }

        if (error) {
            return;
        }

        auto message = parser.release();

        Request request;

        request.method = std::string(message.method_string());

        parse_target(message.target(), request);

        for (const auto& field : message) {
            request.headers.emplace_back(
                std::string(field.name_string()),
                std::string(field.value())
            );
        }

        request.body = std::move(message.body());

        const Response response = router.handle(request);

        const bool keep_alive = message.keep_alive();

        if (!write_response(socket, response, message.version(), keep_alive) || !keep_alive) {
            beast::error_code ignored;

            socket.shutdown(tcp::socket::shutdown_send, ignored);

            return;
        }
    }
}

}  // namespace

// ================================================================
// Server
// ================================================================

Server::Server(
    const Router& router,
    ServerOptions options
)
    : router_(router),
      options_(std::move(options)) {}

void Server::run() {
    net::io_context context;

    const tcp::endpoint endpoint(net::ip::make_address(options_.host), options_.port);

    tcp::acceptor acceptor(context);

    acceptor.open(endpoint.protocol());
    acceptor.set_option(net::socket_base::reuse_address(true));
    acceptor.bind(endpoint);
    acceptor.listen(net::socket_base::max_listen_connections);

    log::info(format("Gateway listening on http://%s:%u", options_.host.c_str(), options_.port));

    while (true) {
        beast::error_code error;

        tcp::socket socket(context);

        acceptor.accept(socket, error);

        if (error) {
            continue;
        }

        socket.set_option(tcp::no_delay(true), error);

        if (connections_.load() >= options_.max_connections) {
            write_error(socket, 503, "Too many connections");
            continue;
        }

        ++connections_;

        auto* shared = new tcp::socket(std::move(socket));

        const bool started = start_detached_thread(
            [this, shared]() {
                std::unique_ptr<tcp::socket> owned(shared);

                try {
                    serve_connection(std::move(*owned), router_, options_);
                } catch (const std::exception& exception) {
                    log::error(format("Connection failed: %s", exception.what()));
                }

                --connections_;
            }
        );

        if (!started) {
            delete shared;
            --connections_;
        }
    }
}

}  // namespace gw::http
