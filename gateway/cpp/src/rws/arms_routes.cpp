#include "app.hpp"
#include "http/validate.hpp"
#include "rws/http_errors.hpp"
#include "util/text.hpp"

namespace gw {

using http::HttpError;
using http::Json;
using http::Request;
using http::Response;

namespace rws {

http::HttpError to_http_error(
    const RwsError& error
) {
    if (!error.status) {
        //
        // The message already says the controller is unreachable.
        //
        return HttpError(503, error.message);
    }

    int status = 502;

    if (*error.status == 400 || *error.status == 404) {
        status = *error.status;
    } else if (*error.status == 403 || *error.status == 409) {
        status = 409;
    }

    return HttpError(status, "Robot controller: " + error.message);
}

}  // namespace rws

namespace {

//
// YuMi mechanical units. ROB_R_7 (the right arm's 7th axis) is
// reported as the arm angle of ROB_R instead.
//
const char* arm_unit(
    const std::string& arm
) {
    return arm == "left" ? "ROB_L" : "ROB_R";
}

//
// RWS sends numbers as text ("300.5"); Pydantic converts them.
//
double as_double(
    const Json& state,
    const char* name
) {
    if (state.contains(name)) {
        const Json& value = state[name];

        if (value.is_number()) {
            return value.get<double>();
        }

        if (value.is_string()) {
            if (const auto number = parse_double(value.get<std::string>())) {
                return *number;
            }
        }
    }

    throw rws::RwsError(502, std::string("Robot controller sent no number for ") + name);
}

long long as_integer(
    const Json& state,
    const char* name
) {
    if (state.contains(name)) {
        const Json& value = state[name];

        if (value.is_number_integer()) {
            return value.get<long long>();
        }

        if (value.is_string()) {
            if (const auto number = parse_integer(value.get<std::string>())) {
                return *number;
            }
        }
    }

    throw rws::RwsError(502, std::string("Robot controller sent no integer for ") + name);
}

std::string as_string(
    const Json& state,
    const char* name
) {
    if (state.contains(name) && state[name].is_string()) {
        return state[name].get<std::string>();
    }

    throw rws::RwsError(502, std::string("Robot controller sent no ") + name);
}

Json read_pose(
    rws::RwsClient& client,
    const std::string& arm,
    std::optional<std::string> tool = std::nullopt,
    std::optional<std::string> wobj = std::nullopt,
    const std::optional<std::string>& coordinate = std::nullopt
) {
    const std::string unit = arm_unit(arm);

    //
    // Without tool or wobj the controller uses the arm's active
    // one, so look up which that is to report it.
    //
    if (!tool || !wobj) {
        const Json mechunit = client.mechunit(unit);

        if (!tool) {
            tool = as_string(mechunit, "tool-name");
        }

        if (!wobj) {
            wobj = as_string(mechunit, "wobj-name");
        }
    }

    const Json target = client.robtarget(unit, tool, wobj, coordinate);

    return Json{
        {"arm", arm},
        {"mech_unit", unit},
        {"tool", *tool},
        {"wobj", *wobj},
        {"coordinate", coordinate ? Json(*coordinate) : Json(nullptr)},
        {"x", as_double(target, "x")},
        {"y", as_double(target, "y")},
        {"z", as_double(target, "z")},
        {"q1", as_double(target, "q1")},
        {"q2", as_double(target, "q2")},
        {"q3", as_double(target, "q3")},
        {"q4", as_double(target, "q4")},
        {"cf1", as_integer(target, "cf1")},
        {"cf4", as_integer(target, "cf4")},
        {"cf6", as_integer(target, "cf6")},
        {"cfx", as_integer(target, "cfx")},
        {"arm_angle", as_double(target, "eaxa")},
    };
}

//
// The {arm} path parameter: left or right, else 422.
//
std::string path_arm(
    const Request& request
) {
    http::Fields path(Json{{"arm", request.param("arm")}}, "path");

    const auto arm = path.choice("arm", {"left", "right"}, std::nullopt);

    path.check();

    return *arm;
}

}  // namespace

void add_arm_routes(
    App& app,
    http::Router& router
) {
    const std::string prefix = app.settings.api_prefix + "/robot/arms";

    // ------------------------------------------------------------
    // GET /robot/arms
    // ------------------------------------------------------------

    router.get(
        prefix,
        [&app](Request& request) {
            app.auth.require_permission(request, "robot.read");

            return rws::call([&]() {
                Json poses = Json::object();

                for (const char* arm : {"left", "right"}) {
                    poses[arm] = read_pose(app.rws, arm);
                }

                return Response::json(poses);
            });
        }
    );

    // ------------------------------------------------------------
    // GET /robot/arms/{arm}/pose
    // ------------------------------------------------------------

    router.get(
        prefix + "/{arm}/pose",
        [&app](Request& request) {
            app.auth.require_permission(request, "robot.read");

            //
            // Path and query problems are reported together.
            //
            http::Fields path(Json{{"arm", request.param("arm")}}, "path");
            http::Fields query(http::query_object(request), "query");

            const auto arm = path.choice("arm", {"left", "right"}, std::nullopt);
            const auto tool = query.rapid_name("tool", false);
            const auto wobj = query.rapid_name("wobj", false);
            const auto coordinate = query.choice("coordinate", {"Base", "Wobj", "Tool"}, "");

            path.check();
            query.check();

            return rws::call([&]() {
                return Response::json(
                    read_pose(
                        app.rws,
                        *arm,
                        tool,
                        wobj,
                        coordinate && !coordinate->empty() ? coordinate : std::nullopt
                    )
                );
            });
        }
    );

    // ------------------------------------------------------------
    // GET /robot/arms/{arm}/joints
    // ------------------------------------------------------------

    router.get(
        prefix + "/{arm}/joints",
        [&app](Request& request) {
            app.auth.require_permission(request, "robot.read");

            const std::string arm = path_arm(request);

            return rws::call([&]() {
                const std::string unit = arm_unit(arm);
                const Json target = app.rws.jointtarget(unit);

                Json joints = Json::array();

                for (int axis = 1; axis <= 6; ++axis) {
                    joints.push_back(as_double(target, format("rax_%d", axis).c_str()));
                }

                return Response::json(
                    Json{
                        {"arm", arm},
                        {"mech_unit", unit},
                        {"joints", joints},
                        {"arm_angle", as_double(target, "eax_a")},
                    }
                );
            });
        }
    );

    // ------------------------------------------------------------
    // GET /robot/arms/{arm}/gripper
    // ------------------------------------------------------------

    router.get(
        prefix + "/{arm}/gripper",
        [&app](Request& request) {
            app.auth.require_permission(request, "robot.read");

            const std::string arm = path_arm(request);

            double position = 0.0;

            try {
                position = app.rws.gripper_position(arm == "left" ? "L" : "R");

            } catch (const rws::RwsError& error) {
                if (error.status == 404) {
                    throw HttpError(404, "The controller has no SmartGripper on the " + arm + " arm");
                }

                throw rws::to_http_error(error);
            }

            return Response::json(
                Json{
                    {"arm", arm},
                    {"position", position},
                }
            );
        }
    );
}

}  // namespace gw
