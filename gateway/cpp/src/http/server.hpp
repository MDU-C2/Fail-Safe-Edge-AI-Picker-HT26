#pragma once

#include <cstddef>
#include <memory>
#include <stdexcept>
#include <string>

#include "http/router.hpp"

namespace gw::http {

// ================================================================
// HTTP server
// ================================================================
//
// cpp-httplib (http/httplib.hpp) serves the router: HTTP(S) and
// WebSockets (WS/WSS), one pooled thread per connection with
// blocking reads and writes. A handler may block (a robot move waits
// up to a minute for the arm) without holding up any other
// connection, so a lease renewal never queues behind a move.
//

struct ServerOptions {
    std::string host = "127.0.0.1";
    unsigned short port = 8000;

    //
    // HTTPS / WSS with this PEM certificate (chain) and unencrypted
    // key when set; plain HTTP / WS otherwise.
    //
    std::string tls_cert_file;
    std::string tls_key_file;

    //
    // Connections served at once (a WebSocket holds one for its
    // whole life); more wait for a free thread.
    //
    std::size_t max_connections = 64;

    //
    // An idle keep-alive connection is closed after this long
    // (uvicorn's default is 5 s).
    //
    int keep_alive_seconds = 5;

    //
    // Largest request body accepted (RAPID sources and calibration
    // are limited to 1 MB of text; JSON escaping can add to that).
    //
    std::size_t max_body_bytes = 4 * 1024 * 1024;
};

class ServerError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

class Server {
public:
    //
    // Throws ServerError if the TLS certificate or key cannot be
    // used.
    //
    Server(
        const Router& router,
        ServerOptions options
    );

    ~Server();

    //
    // Binds and serves until the process ends. Throws ServerError
    // if the address cannot be bound.
    //
    void run();

private:
    struct Impl;

    std::unique_ptr<Impl> impl_;
};

}  // namespace gw::http
