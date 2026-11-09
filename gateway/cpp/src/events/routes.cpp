#include <map>

#include "app.hpp"

namespace gw {

using http::Json;

namespace {

//
// gateway/events/permissions.py: the names that can be subscribed
// to and the permission each needs. A new event family must be
// added here or subscribing to it is rejected as unknown_event.
//
const std::map<std::string, std::string> EVENT_PERMISSIONS = {
    {"control", "robot.read"},
    {"robot.status", "robot.read"},
    {"robot.error", "robot.read"},
    {"robot.rapid", "robot.read"},
    {"camera.status", "camera.read"},
    {"calibration", "camera.read"},
};

std::string error_message(
    const std::string& message
) {
    return http::to_text(Json{{"type", "error"}, {"data", Json{{"message", message}}}});
}

std::string subscriptions_message(
    const std::set<std::string>& subscriptions
) {
    //
    // std::set is sorted, like Python's sorted().
    //
    return http::to_text(
        Json{
            {"type", "subscription.updated"},
            {"data", Json{{"events", Json(subscriptions)}}},
        }
    );
}

//
// How Python's f"{action}" shows a JSON value.
//
std::string python_str(
    const Json& value
) {
    if (value.is_string()) {
        return value.get<std::string>();
    }

    if (value.is_null()) {
        return "None";
    }

    if (value.is_boolean()) {
        return value.get<bool>() ? "True" : "False";
    }

    return http::to_text(value);
}

//
// One message from the client, as the loop body of the Python
// handler; the reply goes back on the same socket.
//
void handle_message(
    const std::string& text,
    EventConnection& connection,
    http::WebSocket& socket
) {
    const Json message = Json::parse(text, nullptr, false);

    if (message.is_discarded()) {
        socket.send(error_message("Message must be JSON"));
        return;
    }

    if (!message.is_object()) {
        socket.send(error_message("Message must be a JSON object"));
        return;
    }

    const Json action = message.contains("action") ? message["action"] : Json(nullptr);
    const Json events = message.contains("events") ? message["events"] : Json::array();

    if (!events.is_array()) {
        socket.send(error_message("'events' must be a list"));
        return;
    }

    std::vector<std::string> names;

    for (const auto& event : events) {
        if (!event.is_string()) {
            socket.send(error_message("Every event name must be a string"));
            return;
        }

        names.push_back(event.get<std::string>());
    }

    if (action == "subscribe") {
        Json rejected = Json::array();

        for (const auto& name : names) {
            const auto permission = EVENT_PERMISSIONS.find(name);

            if (permission == EVENT_PERMISSIONS.end()) {
                rejected.push_back(Json{{"event", name}, {"reason", "unknown_event"}});
                continue;
            }

            if (!connection.identity().can(permission->second)) {
                rejected.push_back(Json{{"event", name}, {"reason", "forbidden"}});
            }
        }

        if (!rejected.empty()) {
            socket.send(
                http::to_text(
                    Json{
                        {"type", "subscription.rejected"},
                        {"data", Json{{"events", rejected}}},
                    }
                )
            );
            return;
        }

        socket.send(subscriptions_message(connection.subscribe(names)));

    } else if (action == "unsubscribe") {
        socket.send(subscriptions_message(connection.unsubscribe(names)));

    } else {
        socket.send(error_message("Unknown action: " + python_str(action)));
    }
}

}  // namespace

void add_event_routes(
    App& app,
    http::Router& router
) {
    //
    // WS /events: development mode only (the client from ?client=,
    // default the highest priority); strict mode refuses it, since
    // token auth is not implemented for the WebSocket.
    //
    router.websocket(
        app.settings.api_prefix + "/events",
        [&app](http::Request& request, const std::shared_ptr<http::WebSocket>& socket)
            -> std::optional<http::WebSocketCallbacks> {
            if (!app.auth.development()) {
                return std::nullopt;
            }

            const auto identity = app.auth.development_identity(request.query_value("client"));

            if (!identity) {
                return std::nullopt;
            }

            //
            // The bus holds the connection, the connection only a
            // weak reference to the socket: no cycle once it ends.
            //
            const std::weak_ptr<http::WebSocket> weak = socket;

            auto connection = app.events.connect(
                *identity,
                [weak](const std::string& text) {
                    const auto alive = weak.lock();

                    return alive && alive->send(text);
                }
            );

            http::WebSocketCallbacks callbacks;

            callbacks.on_message = [connection, weak](const std::string& text) {
                if (const auto alive = weak.lock()) {
                    handle_message(text, *connection, *alive);
                }
            };

            callbacks.on_close = [&app, connection]() {
                app.events.disconnect(connection);
            };

            return callbacks;
        }
    );
}

}  // namespace gw
