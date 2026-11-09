#include <cmath>

#include "app.hpp"
#include "util/text.hpp"

namespace gw {

using http::HttpError;
using http::Json;
using http::Request;
using http::Response;

namespace {

Json serialize_lease(
    const std::optional<ControlLease>& lease
) {
    if (!lease) {
        return Json{
            {"active", false},
            {"client_id", nullptr},
            {"priority", nullptr},
            {"acquired_at", nullptr},
            {"expires_at", nullptr},
        };
    }

    return Json{
        {"active", true},
        {"client_id", lease->client_id},
        {"priority", lease->priority},
        {"acquired_at", format_json_time(lease->acquired_at)},
        {"expires_at", format_json_time(lease->expires_at)},
    };
}

Response action(
    const std::string& message,
    const std::optional<ControlLease>& lease
) {
    return Response::json(
        Json{
            {"success", true},
            {"message", message},
            {"lease", serialize_lease(lease)},
        }
    );
}

}  // namespace

void add_control_routes(
    App& app,
    http::Router& router
) {
    const std::string prefix = app.settings.api_prefix + "/control";

    // ------------------------------------------------------------
    // GET /control
    // ------------------------------------------------------------

    router.get(
        prefix,
        [&app](Request& request) {
            app.auth.require_permission(request, "robot.read");

            return Response::json(serialize_lease(app.control.get()));
        }
    );

    // ------------------------------------------------------------
    // POST /control/acquire
    // ------------------------------------------------------------

    router.post(
        prefix + "/acquire",
        [&app](Request& request) {
            const Identity identity = app.auth.require_permission(request, "robot.control");

            const AcquireResult result = app.control.acquire(identity.client_id, identity.control_priority);

            if (!result.success) {
                http::Headers headers;

                if (result.retry_after) {
                    const double seconds = std::max(1.0, std::ceil(*result.retry_after));

                    headers.emplace_back("Retry-After", format("%.0f", seconds));
                }

                throw HttpError(409, result.message, headers);
            }

            if (result.previous_client_id) {
                app.events.send_to_client(
                    *result.previous_client_id,
                    Json{
                        {"type", "control.revoked"},
                        {
                            "data",
                            Json{
                                {"reason", "preempted"},
                                {"new_controller", identity.client_id},
                            },
                        },
                    }
                );
            }

            app.events.publish(
                "control.acquired",
                Json{
                    {"client_id", identity.client_id},
                    {"priority", identity.control_priority},
                }
            );

            return action(result.message, result.lease);
        }
    );

    // ------------------------------------------------------------
    // POST /control/renew
    // ------------------------------------------------------------

    router.post(
        prefix + "/renew",
        [&app](Request& request) {
            const Identity identity = app.auth.require_permission(request, "robot.control");

            const RenewResult result = app.control.renew(identity.client_id, identity.control_priority);

            if (!result.success) {
                throw HttpError(409, result.message);
            }

            app.events.publish(
                "control.renewed",
                Json{
                    {"client_id", identity.client_id},
                }
            );

            return action(result.message, result.lease);
        }
    );

    // ------------------------------------------------------------
    // POST /control/release
    // ------------------------------------------------------------

    router.post(
        prefix + "/release",
        [&app](Request& request) {
            const Identity identity = app.auth.require_permission(request, "robot.control");

            const ReleaseResult result = app.control.release(identity.client_id);

            if (!result.success) {
                throw HttpError(409, result.message);
            }

            app.events.publish(
                "control.released",
                Json{
                    {"client_id", identity.client_id},
                }
            );

            return action(result.message, std::nullopt);
        }
    );
}

}  // namespace gw
