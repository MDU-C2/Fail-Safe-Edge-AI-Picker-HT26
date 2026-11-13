#pragma once

#include <functional>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include "http/message.hpp"

namespace gw::http {

// ================================================================
// WebSockets
// ================================================================
//
// A WebSocket route has two steps. Before the handshake, authorize
// looks at the upgrade request; false refuses it with HTTP 403, as
// uvicorn answers a Starlette WebSocket closed before it was
// accepted. After the handshake, open gets the request and the
// sending side of the connection and returns the callbacks (nothing
// closes the connection again with 1008).
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

using WebSocketAuthorizer = std::function<bool(Request& request)>;

using WebSocketHandler = std::function<
    std::optional<WebSocketCallbacks>(
        Request& request,
        const std::shared_ptr<WebSocket>& socket
    )
>;

struct WebSocketRoute {
    std::vector<std::string> parts;

    WebSocketAuthorizer authorize;

    WebSocketHandler open;
};

}  // namespace gw::http
