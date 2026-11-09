#pragma once

#include <atomic>
#include <cstddef>
#include <string>

#include "http/router.hpp"

namespace gw::http {

// ================================================================
// HTTP server
// ================================================================
//
// One thread per connection, blocking reads and writes. A handler
// may block (a robot move waits up to a minute for the arm) without
// holding up any other connection, so a lease renewal never queues
// behind a move. Threads get a small stack and their number is
// capped.
//

struct ServerOptions {
    std::string host = "127.0.0.1";
    unsigned short port = 8000;

    //
    // Connections beyond this are closed at once.
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

class Server {
public:
    Server(
        const Router& router,
        ServerOptions options
    );

    //
    // Binds and accepts until the process ends. Throws if the
    // address cannot be bound.
    //
    void run();

private:
    const Router& router_;

    ServerOptions options_;

    std::atomic<std::size_t> connections_{0};
};

}  // namespace gw::http
