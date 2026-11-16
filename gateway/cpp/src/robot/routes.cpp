#include "app.hpp"

namespace gw {

using http::Request;
using http::Response;

void add_robot_routes(
    App& app,
    http::Router& router
) {
    const std::string prefix = app.settings.api_prefix + "/robot";

    router.get(
        prefix + "/status",
        [&app](Request& request) {
            app.auth.require_permission(request, "robot.read");

            return Response::json(app.robot->status());
        }
    );
}

}  // namespace gw
