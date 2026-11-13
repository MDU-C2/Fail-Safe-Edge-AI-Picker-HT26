#pragma once

#include <functional>
#include <string>
#include <vector>

#include "http/message.hpp"
#include "http/websocket.hpp"

namespace gw::http {

using Handler = std::function<Response(Request&)>;

// ================================================================
// Router
// ================================================================
//
// Routes match in the order they were added, like Starlette. A
// pattern part {name} matches one path segment. A path that matches
// a route with another method gives 405, an unknown path 404, both
// with FastAPI's bodies.
//

class Router {
public:
    void add(
        std::string method,
        std::string pattern,
        Handler handler
    );

    void get(
        std::string pattern,
        Handler handler
    );

    void post(
        std::string pattern,
        Handler handler
    );

    void put(
        std::string pattern,
        Handler handler
    );

    void del(
        std::string pattern,
        Handler handler
    );

    //
    // A WebSocket route (GET with an Upgrade: websocket header).
    //
    void websocket(
        std::string pattern,
        WebSocketAuthorizer authorize,
        WebSocketHandler open
    );

    //
    // Runs the matching handler and turns HttpError and other
    // exceptions into responses; never throws.
    //
    Response handle(
        Request& request
    ) const;

    //
    // The WebSocket route for the request's path (its parameters
    // filled in), or null.
    //
    const WebSocketRoute* find_websocket(
        Request& request
    ) const;

private:
    struct Route {
        std::string method;
        std::vector<std::string> parts;
        Handler handler;
    };

    static bool match_parts(
        const std::vector<std::string>& parts,
        const std::vector<std::string>& segments,
        std::map<std::string, std::string>& params
    );

    std::vector<WebSocketRoute> websocket_routes_;

    static bool match(
        const Route& route,
        const std::vector<std::string>& segments,
        std::map<std::string, std::string>& params
    );

    std::vector<Route> routes_;
};

//
// "/a/b/c" -> {"a", "b", "c"}; the parts are percent-decoded.
//
std::vector<std::string> split_path(
    const std::string& path
);

}  // namespace gw::http
