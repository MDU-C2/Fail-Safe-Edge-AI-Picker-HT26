#include "app.hpp"
#include "http/validate.hpp"
#include "rws/http_errors.hpp"

namespace gw {

using http::Json;
using http::Request;
using http::Response;

namespace {

//
// The state of the robot.status event for a reply, as the Python
// gateway's COMMAND_STATES (shared with the legacy provider).
//
const char* command_state(
    const std::string& message
) {
    if (message == "STARTED") {
        return "command_started";
    }

    if (message == "BUSY") {
        return "busy";
    }

    if (message == "DONE") {
        return "command_completed";
    }

    return "command_failed";
}

Json result_json(
    const rws::CommandResult& result
) {
    return Json{
        {"accepted", result.accepted},
        {"message", result.message},
    };
}

//
// A job's reply, after the robot.status event.
//
Response job_response(
    App& app,
    const rws::CommandResult& result
) {
    app.events.publish(
        "robot.status",
        Json{
            {"connected", true},
            {"state", command_state(result.message)},
        }
    );

    return Response::json(result_json(result));
}

rws::Arm to_arm(
    const std::string& name
) {
    return name == "right" ? rws::Arm::right : rws::Arm::left;
}

//
// The fields every job has (RwsArmRequest): arm and wait.
//
struct ArmFields {
    rws::Arm arm = rws::Arm::left;
    bool wait = true;
};

ArmFields arm_fields(
    http::Fields& fields
) {
    ArmFields result;

    result.arm = to_arm(fields.choice("arm", {"left", "right"}, "left").value_or("left"));
    result.wait = fields.boolean("wait", true);

    return result;
}

//
// RwsMotionRequest: speed, acceleration and ramp; what the request
// leaves out is the gateway's default.
//
rws::MotionSpeed motion_speed(
    http::Fields& fields,
    const rws::MotionSpeed& defaults
) {
    rws::MotionSpeed speed = defaults;

    //
    // RAPID AccSet treats less than 20 % / 10 % as 20 % / 10 %, so
    // that is refused.
    //
    const auto tcp = fields.number("speed", false, {1.0, std::nullopt, 1500.0});
    const auto acceleration = fields.number("acceleration", false, {20.0, std::nullopt, 100.0});
    const auto ramp = fields.number("ramp", false, {10.0, std::nullopt, 100.0});

    speed.speed = tcp.value_or(defaults.speed);
    speed.acceleration = acceleration.value_or(defaults.acceleration);
    speed.ramp = ramp.value_or(defaults.ramp);

    return speed;
}

}  // namespace

void add_motion_routes(
    App& app,
    http::Router& router
) {
    const std::string prefix = app.settings.api_prefix + "/robot/rws";

    // ------------------------------------------------------------
    // POST /robot/rws/move
    // ------------------------------------------------------------

    router.post(
        prefix + "/move",
        [&app](Request& request) {
            app.auth.require_control(request, "robot.control");

            http::Fields body(http::object_body(request), "body");

            const ArmFields job = arm_fields(body);
            const rws::MotionSpeed speed = motion_speed(body, app.motion.default_speed());

            const auto x = body.number("x", true);
            const auto y = body.number("y", true);
            const auto z = body.number("z", true);

            const auto rx = body.number("rx", false);
            const auto ry = body.number("ry", false);
            const auto rz = body.number("rz", false);

            const int given = int(rx.has_value()) + int(ry.has_value()) + int(rz.has_value());

            if (given != 0 && given != 3) {
                body.model_error("Give all of rx, ry and rz, or none of them");
            }

            body.check();

            std::optional<rws::Vector3> rotation;

            if (given == 3) {
                rotation = rws::Vector3{*rx, *ry, *rz};
            }

            const auto result = rws::call([&]() {
                return app.motion.move(job.arm, {*x, *y, *z}, rotation, job.wait, speed);
            });

            return job_response(app, result);
        }
    );

    // ------------------------------------------------------------
    // POST /robot/rws/home (body optional)
    // ------------------------------------------------------------

    router.post(
        prefix + "/home",
        [&app](Request& request) {
            app.auth.require_control(request, "robot.control");

            http::Fields body(http::object_body(request, true), "body");

            const ArmFields job = arm_fields(body);
            const rws::MotionSpeed speed = motion_speed(body, app.motion.default_speed());

            body.check();

            const auto result = rws::call([&]() {
                return app.motion.home(job.arm, job.wait, speed);
            });

            return job_response(app, result);
        }
    );

    // ------------------------------------------------------------
    // POST /robot/rws/gripper
    // ------------------------------------------------------------

    router.post(
        prefix + "/gripper",
        [&app](Request& request) {
            app.auth.require_control(request, "robot.control");

            http::Fields body(http::object_body(request), "body");

            const ArmFields job = arm_fields(body);

            const auto action = body.choice("action", {"grip", "open", "move"}, std::nullopt);
            const auto position = body.number("position", false, {0.0, std::nullopt, 25.0});
            const auto force = body.number("force", false, {std::nullopt, 0.0, 20.0});

            if (body.ok()) {
                if ((*action == "move") != position.has_value()) {
                    body.model_error("position is required for move and only for move");
                } else if (force && *action != "grip") {
                    body.model_error("force is only for grip");
                }
            }

            body.check();

            const rws::GripperAction gripper_action =
                *action == "grip" ? rws::GripperAction::grip
                : *action == "open" ? rws::GripperAction::open
                : rws::GripperAction::move;

            const auto result = rws::call([&]() {
                return app.motion.gripper(job.arm, gripper_action, job.wait, position, force);
            });

            return job_response(app, result);
        }
    );

    // ------------------------------------------------------------
    // GET /robot/rws/status?arm= (no lease)
    // ------------------------------------------------------------

    router.get(
        prefix + "/status",
        [&app](Request& request) {
            app.auth.require_permission(request, "robot.read");

            http::Fields query(http::query_object(request), "query");

            const auto arm = query.choice("arm", {"left", "right"}, "left");

            query.check();

            const auto result = rws::call([&]() { return app.motion.status(to_arm(*arm)); });

            return Response::json(result_json(result));
        }
    );
}

}  // namespace gw
