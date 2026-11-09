#pragma once

#include <functional>
#include <memory>
#include <optional>
#include <string>

#include "http/message.hpp"

namespace gw::http {

// ================================================================
// WebSockets
// ================================================================
//
// A WebSocket route gets the upgrade request and the sending side of
// the connection. It returns the callbacks for the connection, or
// nothing to refuse it: the client then gets HTTP 403 instead of the
// handshake, as uvicorn answers a Starlette WebSocket closed before
// it was accepted.
//

class WebSocket {
public:
    virtual ~WebSocket() = default;

    //
    // Queues a text message; never blocks, may be called from any
    // thread. False once the connection is closed (or its queue is
    // full, which closes it): the caller should forget it.
    //
    virtual bool send(
        std::string text
    ) = 0;
};

struct WebSocketCallbacks {
    //
    // A text message from the client; runs on the connection's own
    // thread.
    //
    std::function<void(const std::string& text)> on_message;

    //
    // Once, when the connection has ended for any reason.
    //
    std::function<void()> on_close;
};

using WebSocketHandler = std::function<
    std::optional<WebSocketCallbacks>(
        Request& request,
        const std::shared_ptr<WebSocket>& socket
    )
>;

}  // namespace gw::http
