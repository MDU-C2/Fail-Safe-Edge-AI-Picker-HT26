#include "http/server.hpp"

#include <chrono>
#include <deque>
#include <filesystem>
#include <mutex>

#include "http/httplib.hpp"
#include "util/log.hpp"
#include "util/text.hpp"

namespace gw::http {

namespace {

// ================================================================
// Settings of the WebSocket loop
// ================================================================

//
// How often a WebSocket's thread stops waiting for the client to send
// what was queued for it, i.e. the longest an event waits.
//
constexpr auto WEBSOCKET_SEND_INTERVAL = std::chrono::milliseconds(50);

//
// Messages waiting for a slow client; beyond this it is dropped.
//
constexpr std::size_t WEBSOCKET_MAX_QUEUED = 1000;

//
// The server pings every 10 s and drops a client that misses two
// pongs, so a vanished client frees its thread within ~30 s.
//
constexpr time_t WEBSOCKET_PING_SECONDS = 10;
constexpr int WEBSOCKET_MAX_MISSED_PONGS = 2;

// ================================================================
// Requests and responses
// ================================================================

//
// Path and query from the raw request target, as the router has
// always had them (the path is not percent-decoded; a repeated query
// parameter keeps its last value).
//
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

            request.query[key] = value;
        }

        if (amp == std::string_view::npos) {
            break;
        }

        query.remove_prefix(amp + 1);
    }
}

Request to_request(
    const httplib::Request& from
) {
    Request request;

    request.method = from.method;

    parse_target(from.target, request);

    for (const auto& [name, value] : from.headers) {
        request.headers.emplace_back(name, value);
    }

    request.body = from.body;

    return request;
}

void to_response(
    const Response& from,
    httplib::Response& to
) {
    to.status = from.status;

    for (const auto& [name, value] : from.headers) {
        to.set_header(name, value);
    }

    if (!from.shared_body) {
        to.set_content(from.body, from.content_type);
        return;
    }

    //
    // Camera frames: sent from the shared buffer, not copied.
    //
    const SharedBytes body = from.shared_body;

    to.set_content_provider(
        body.size,
        from.content_type,
        [body](std::size_t offset, std::size_t length, httplib::DataSink& sink) {
            return sink.write(reinterpret_cast<const char*>(body.data) + offset, length);
        }
    );
}

bool is_websocket_upgrade(
    const httplib::Request& request
) {
    return iequals(request.get_header_value("Upgrade"), "websocket");
}

// ================================================================
// WebSockets
// ================================================================

//
// The sending side handed to a WebSocket route. send() only queues,
// so publishing an event never waits for a slow client; the
// connection's own thread sends what is queued.
//
class QueuedWebSocket : public WebSocket {
public:
    bool send(
        std::string text
    ) override {
        const std::lock_guard lock(mutex_);

        if (closed_) {
            return false;
        }

        if (queue_.size() >= WEBSOCKET_MAX_QUEUED) {
            log::warning("WebSocket client too slow, closing the connection");

            closed_ = true;
            return false;
        }

        queue_.push_back(std::move(text));

        return true;
    }

    //
    // What is queued; false once the connection is to be closed.
    //
    bool take(
        std::deque<std::string>& messages
    ) {
        const std::lock_guard lock(mutex_);

        messages.swap(queue_);

        return !closed_;
    }

    void close() {
        const std::lock_guard lock(mutex_);

        closed_ = true;
        queue_.clear();
    }

private:
    std::mutex mutex_;

    std::deque<std::string> queue_;

    bool closed_ = false;
};

//
// One WebSocket connection, on its own thread, after the handshake.
//
void serve_websocket(
    const Router& router,
    const httplib::Request& upgrade,
    httplib::ws::WebSocket& client
) {
    Request request = to_request(upgrade);

    const WebSocketRoute* route = router.find_websocket(request);

    auto socket = std::make_shared<QueuedWebSocket>();

    std::optional<WebSocketCallbacks> callbacks;

    try {
        if (route != nullptr) {
            callbacks = route->open(request, socket);
        }
    } catch (const std::exception& exception) {
        log::error(format("WebSocket handler failed: %s", exception.what()));
    }

    if (!callbacks) {
        client.close(httplib::ws::CloseStatus::PolicyViolation);
        return;
    }

    client.set_read_timeout(WEBSOCKET_SEND_INTERVAL);

    std::deque<std::string> outgoing;
    std::string message;

    while (client.is_open()) {
        const bool open = socket->take(outgoing);

        bool sent = true;

        for (const std::string& text : outgoing) {
            if (!client.send(text)) {
                sent = false;
                break;
            }
        }

        outgoing.clear();

        if (!open || !sent) {
            client.close(httplib::ws::CloseStatus::GoingAway);
            break;
        }

        const auto result = client.read(message);

        if (result == httplib::ws::ReadResult::Timeout) {
            continue;
        }

        if (result == httplib::ws::ReadResult::Fail) {
            break;
        }

        if (result == httplib::ws::ReadResult::Text && callbacks->on_message) {
            try {
                callbacks->on_message(message);
            } catch (const std::exception& exception) {
                log::error(format("WebSocket message handler failed: %s", exception.what()));
            }
        }
    }

    socket->close();

    if (callbacks->on_close) {
        callbacks->on_close();
    }
}

// ================================================================
// TLS
// ================================================================

std::unique_ptr<httplib::Server> create_server(
    const ServerOptions& options
) {
    if (options.tls_cert_file.empty()) {
        return std::make_unique<httplib::Server>();
    }

    for (const auto& [setting, file] : {
        std::pair{"GATEWAY_TLS_CERT_FILE", options.tls_cert_file},
        std::pair{"GATEWAY_TLS_KEY_FILE", options.tls_key_file},
    }) {
        if (!std::filesystem::is_regular_file(file)) {
            throw ServerError(format("%s=%s: no such file", setting, file.c_str()));
        }
    }

    //
    // A password that no real key has: an encrypted key then fails to
    // load instead of OpenSSL asking for its passphrase on a terminal
    // the device does not have.
    //
    auto server = std::make_unique<httplib::SSLServer>(
        options.tls_cert_file.c_str(),
        options.tls_key_file.c_str(),
        nullptr,
        nullptr,
        "-"
    );

    if (!server->is_valid()) {
        throw ServerError(
            format(
                "cannot use GATEWAY_TLS_CERT_FILE=%s with GATEWAY_TLS_KEY_FILE=%s: "
                "they must be PEM, the key unencrypted and belonging to the certificate",
                options.tls_cert_file.c_str(),
                options.tls_key_file.c_str()
            )
        );
    }

    return server;
}

}  // namespace

// ================================================================
// Server
// ================================================================

struct Server::Impl {
    ServerOptions options;

    std::unique_ptr<httplib::Server> server;
};

Server::Server(
    const Router& router,
    ServerOptions options
)
    : impl_(std::make_unique<Impl>()) {
    impl_->options = std::move(options);
    impl_->server = create_server(impl_->options);

    httplib::Server& server = *impl_->server;
    const ServerOptions& settings = impl_->options;

    server.new_task_queue = [max = settings.max_connections] {
        return new httplib::ThreadPool(8, max);
    };

    server.set_keep_alive_timeout(settings.keep_alive_seconds);
    server.set_keep_alive_max_count(1000);
    server.set_payload_max_length(settings.max_body_bytes);
    server.set_tcp_nodelay(true);
    server.set_websocket_ping_interval(WEBSOCKET_PING_SECONDS);
    server.set_websocket_max_missed_pongs(WEBSOCKET_MAX_MISSED_PONGS);

    //
    // Every request goes to the router, which matches paths itself.
    //
    const auto handle = [&router](const httplib::Request& from, httplib::Response& to) {
        Request request = to_request(from);

        to_response(router.handle(request), to);
    };

    server.Get(".*", handle);
    server.Post(".*", handle);
    server.Put(".*", handle);
    server.Patch(".*", handle);
    server.Delete(".*", handle);
    server.Options(".*", handle);

    //
    // A WebSocket upgrade the route does not accept (or to a path
    // without one) is refused with 403 before the handshake.
    //
    server.set_pre_request_handler(
        [&router](const httplib::Request& from, httplib::Response& to) {
            if (!is_websocket_upgrade(from)) {
                return httplib::Server::HandlerResponse::Unhandled;
            }

            Request request = to_request(from);

            const WebSocketRoute* route = router.find_websocket(request);

            if (route != nullptr && route->authorize(request)) {
                return httplib::Server::HandlerResponse::Unhandled;
            }

            to.status = 403;
            to.set_content("Forbidden", "text/plain; charset=utf-8");

            return httplib::Server::HandlerResponse::Handled;
        }
    );

    server.WebSocket(
        ".*",
        [&router](const httplib::Request& upgrade, httplib::ws::WebSocket& client) {
            serve_websocket(router, upgrade, client);
        }
    );

    //
    // httplib's own errors (malformed request, body too large) as
    // FastAPI's {"detail": ...}; the router's responses pass as they
    // are.
    //
    server.set_error_handler(
        [](const httplib::Request&, httplib::Response& response) {
            if (!response.body.empty()) {
                return httplib::Server::HandlerResponse::Unhandled;
            }

            to_response(error_response(response.status, httplib::status_message(response.status)), response);

            return httplib::Server::HandlerResponse::Handled;
        }
    );
}

Server::~Server() = default;

void Server::run() {
    const ServerOptions& options = impl_->options;

    if (!impl_->server->bind_to_port(options.host, options.port)) {
        throw ServerError(format("cannot listen on %s:%u", options.host.c_str(), options.port));
    }

    log::info(
        format(
            "Gateway listening on %s://%s:%u",
            options.tls_cert_file.empty() ? "http" : "https",
            options.host.c_str(),
            options.port
        )
    );

    impl_->server->listen_after_bind();
}

}  // namespace gw::http
