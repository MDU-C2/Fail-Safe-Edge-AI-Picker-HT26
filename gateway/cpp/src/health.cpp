#include <cmath>

#include "app.hpp"

namespace gw {

using http::Json;
using http::Request;
using http::Response;

namespace {

//
// The health check must answer quickly even when the controller is
// off the network, so RWS gets much shorter (connect, read) timeouts
// than usual. A controller on the LAN connects in milliseconds.
//
constexpr rws::Timeout PROBE_TIMEOUT{0.5, 2.0};

double round_to(
    double value,
    int digits
) {
    const double scale = std::pow(10.0, digits);

    return std::round(value * scale) / scale;
}

Json camera_health(
    const App& app
) {
    const auto frame_set = app.camera.latest();
    const bool connected = app.camera.is_connected();
    const double max_age = app.settings.camera_max_frame_age_seconds;

    std::optional<double> age;

    if (frame_set) {
        const SteadyTime received = frame_set->rgb
            ? frame_set->rgb->received
            : frame_set->depth ? frame_set->depth->received : app.camera.clock().steady();

        age = to_seconds(app.camera.clock().steady() - received);
    }

    const auto error = app.camera.error();

    return Json{
        {"ok", connected && age && *age <= max_age},
        {"provider", app.settings.camera_provider},
        {"connected", connected},
        {"frame_age_seconds", age ? Json(round_to(*age, 3)) : Json(nullptr)},
        {"max_frame_age_seconds", max_age},
        {"error", error ? Json(*error) : Json(nullptr)},
    };
}

Json robot_controller_health(
    App& app
) {
    const bool moving = app.motion.is_moving();

    try {
        //
        // The first call finds out whether the controller is there
        // at all; the others are only made if it is.
        //
        const std::string controller_state = app.rws.controller_state(PROBE_TIMEOUT);
        const std::string operation_mode = app.rws.operation_mode(PROBE_TIMEOUT);
        const std::string execution_state = app.rws.execution_state(PROBE_TIMEOUT);

        return Json{
            {"ok", true},
            {"reachable", true},
            {"controller_state", controller_state},
            {"operation_mode", operation_mode},
            {"rapid_execution_state", execution_state},
            {"move_in_progress", moving},
            {"error", nullptr},
        };

    } catch (const rws::RwsError& error) {
        return Json{
            {"ok", false},
            {"reachable", error.status.has_value()},
            {"controller_state", nullptr},
            {"operation_mode", nullptr},
            {"rapid_execution_state", nullptr},
            {"move_in_progress", moving},
            {"error", error.message},
        };
    }
}

}  // namespace

void add_health_routes(
    App& app,
    http::Router& router
) {
    //
    // No authentication; 200 while the gateway runs.
    //
    router.get(
        app.settings.api_prefix + "/health",
        [&app](Request&) {
            const Json camera = camera_health(app);
            const Json robot_controller = robot_controller_health(app);

            const bool ok = camera["ok"].get<bool>() && robot_controller["ok"].get<bool>();

            return Response::json(
                Json{
                    {"status", ok ? "ok" : "degraded"},
                    {"version", GATEWAY_VERSION},
                    {"implementation", "cpp"},
                    {"uptime_seconds", round_to(to_seconds(std::chrono::steady_clock::now() - app.started), 1)},
                    {"security_mode", app.settings.security_mode},
                    {"tls", Json{{"enabled", app.tls_enabled}}},
                    {"robot_provider", app.settings.robot_provider},
                    {"control_lease_active", app.control.get().has_value()},
                    {"camera", camera},
                    {"robot_controller", robot_controller},
                }
            );
        }
    );
}

}  // namespace gw
