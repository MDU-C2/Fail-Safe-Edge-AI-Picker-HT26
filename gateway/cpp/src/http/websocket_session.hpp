#pragma once

#include <boost/asio/ip/tcp.hpp>
#include <boost/beast/http.hpp>

#include "http/websocket.hpp"

namespace gw::http {

// ================================================================
// WebSocket session (internal to the server)
// ================================================================

struct WebSocketOptions {
    //
    // The server pings an idle client and closes the connection if
    // nothing comes back within this time, so a vanished client does
    // not keep its thread.
    //
    int idle_timeout_seconds = 20;

    //
    // Messages waiting for a slow client; beyond this it is closed.
    //
    std::size_t max_queued_messages = 1000;

    std::size_t max_message_bytes = 64 * 1024;
};

//
// Runs one WebSocket connection on the calling thread until it ends:
// asks the handler (403 if it refuses), does the handshake, delivers
// messages, sends what the handler queues. The socket's own blocking
// thread drives a private event loop, so sends from other threads
// never block them.
//
void run_websocket(
    boost::asio::ip::tcp::socket socket,
    boost::beast::http::request<boost::beast::http::string_body> upgrade,
    Request request,
    const WebSocketHandler& handler,
    const WebSocketOptions& options
);

}  // namespace gw::http
