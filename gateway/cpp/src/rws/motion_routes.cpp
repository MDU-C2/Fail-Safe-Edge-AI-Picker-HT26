#include <stdexcept>

#include "app.hpp"
#include "http/validate.hpp"
#include "rws/http_errors.hpp"
#include "rws/queue.hpp"

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

// ================================================================
// Job bodies
// ================================================================
//
// The body of POST /robot/rws/move|path|home|gripper, read into a
// QueueJob: the same rules for a single job and for a job of a
// queue (where wait is ignored: a queue always waits).
//

struct ParsedJob {
    rws::QueueJob job;

    bool wait = true;
};

void parse_move(
    http::Fields& body,
    rws::QueueJob& job
) {
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

    if (!body.ok()) {
        return;
    }

    job.position = {*x, *y, *z};

    if (given == 3) {
        job.rotation = rws::Vector3{*rx, *ry, *rz};
    }
}

void parse_path(
    http::Fields& body,
    rws::QueueJob& job
) {
    const auto items = body.list("points", true, 1, rws::RwsMotion::MAX_PATH_POINTS);

    for (std::size_t i = 0; items && i < items->size(); ++i) {
        http::Fields fields = body.item("points", i, "RwsPathPoint");

        rws::PathPoint point;

        const auto x = fields.number("x", true);
        const auto y = fields.number("y", true);
        const auto z = fields.number("z", true);

        const auto rx = fields.number("rx", true);
        const auto ry = fields.number("ry", true);
        const auto rz = fields.number("rz", true);

        point.linear = fields.boolean("linear", false);
        point.speed = fields.number("speed", false, {1.0, std::nullopt, 1500.0});
        point.zone = fields.number("zone", false, {0.0, std::nullopt, 100.0});

        if (fields.ok()) {
            point.position = {*x, *y, *z};
            point.rotation = {*rx, *ry, *rz};

            job.points.push_back(point);
        }

        body.merge(fields);
    }

    job.zone = body.number("zone", false, {0.0, std::nullopt, 100.0}).value_or(10.0);
    job.max_acceleration = body.number("max_acceleration", false, {0.1, std::nullopt, 20.0});
}

void parse_gripper(
    http::Fields& body,
    rws::QueueJob& job
) {
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

    if (!body.ok()) {
        return;
    }

    job.action =
        *action == "grip" ? rws::GripperAction::grip
        : *action == "open" ? rws::GripperAction::open
        : rws::GripperAction::move;

    job.grip_position = position;
    job.force = force;
}

//
// Collects the problems in body; the caller checks it.
//
ParsedJob parse_job(
    rws::QueueJobType type,
    http::Fields& body,
    const rws::MotionSpeed& defaults
) {
    ParsedJob parsed;

    parsed.job.type = type;

    //
    // The fields every job has (RwsArmRequest): arm and wait.
    //
    parsed.job.arm = to_arm(body.choice("arm", {"left", "right"}, "left").value_or("left"));
    parsed.wait = body.boolean("wait", true);

    if (type != rws::QueueJobType::gripper) {
        parsed.job.speed = motion_speed(body, defaults);
    }

    switch (type) {
    case rws::QueueJobType::move:
        parse_move(body, parsed.job);
        break;

    case rws::QueueJobType::path:
        parse_path(body, parsed.job);
        break;

    case rws::QueueJobType::home:
        break;

    case rws::QueueJobType::gripper:
        parse_gripper(body, parsed.job);
        break;
    }

    return parsed;
}

//
// One job now, unless a queue runs (409).
//
Response run_single(
    App& app,
    const ParsedJob& parsed
) {
    const rws::QueueJob& job = parsed.job;

    const auto result = rws::call([&]() {
        const auto admitted = app.queue.admit_single();

        switch (job.type) {
        case rws::QueueJobType::move:
            return app.motion.move(job.arm, job.position, job.rotation, parsed.wait, job.speed);

        case rws::QueueJobType::path:
            return app.motion.path(job.arm, job.points, job.zone, job.max_acceleration, parsed.wait, job.speed);

        case rws::QueueJobType::home:
            return app.motion.home(job.arm, parsed.wait, job.speed);

        case rws::QueueJobType::gripper:
            break;
        }

        return app.motion.gripper(job.arm, job.action, parsed.wait, job.grip_position, job.force);
    });

    return job_response(app, result);
}

// ================================================================
// Queue
// ================================================================

Json optional_json(
    const std::optional<std::string>& value
) {
    return value ? Json(*value) : Json(nullptr);
}

Json time_json(
    const std::optional<WallTime>& time
) {
    return time ? Json(format_json_time(*time)) : Json(nullptr);
}

Json queue_json(
    const rws::QueueSnapshot& queue
) {
    Json steps = Json::array();

    for (const rws::QueueJobStatus& job : queue.jobs) {
        steps.push_back(
            Json{
                {"name", job.name},
                {"type", rws::job_type_name(job.type)},
                {"arm", rws::arm_name(job.arm)},
                {"group", optional_json(job.group)},
                {"state", rws::job_state_name(job.state)},
                {"message", optional_json(job.message)},
            }
        );
    }

    return Json{
        {"state", rws::queue_state_name(queue.state)},
        {"owner", optional_json(queue.owner)},
        {"length", queue.jobs.size()},
        {"step", queue.step ? Json(*queue.step) : Json(nullptr)},
        {"cancel_requested", queue.cancel_requested},
        {"message", optional_json(queue.message)},
        {"started_at", time_json(queue.started_at)},
        {"finished_at", time_json(queue.finished_at)},
        {"steps", steps},
    };
}

}  // namespace

void add_motion_routes(
    App& app,
    http::Router& router
) {
    const std::string prefix = app.settings.api_prefix + "/robot/rws";

    // ------------------------------------------------------------
    // POST /robot/rws/move | path | home (body optional) | gripper
    // ------------------------------------------------------------

    struct SingleRoute {
        const char* path;
        rws::QueueJobType type;
        bool optional_body;
    };

    for (const SingleRoute route : {
             SingleRoute{"/move", rws::QueueJobType::move, false},
             SingleRoute{"/path", rws::QueueJobType::path, false},
             SingleRoute{"/home", rws::QueueJobType::home, true},
             SingleRoute{"/gripper", rws::QueueJobType::gripper, false},
         }) {
        router.post(
            prefix + route.path,
            [&app, route](Request& request) {
                app.auth.require_control(request, "robot.control");

                http::Fields body(http::object_body(request, route.optional_body), "body");

                const ParsedJob parsed = parse_job(route.type, body, app.motion.default_speed());

                body.check();

                return run_single(app, parsed);
            }
        );
    }

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

    // ------------------------------------------------------------
    // Queue: robot.queue.<event> events
    // ------------------------------------------------------------

    app.queue.set_listener([&app](const std::string& event, const rws::QueueSnapshot& queue) {
        app.events.publish("robot.queue." + event, queue_json(queue));
    });

    // ------------------------------------------------------------
    // POST /robot/rws/queue: check every step, then run them all
    // ------------------------------------------------------------

    router.post(
        prefix + "/queue",
        [&app](Request& request) {
            const Identity identity = app.auth.require_control(request, "robot.control");

            http::Fields body(http::object_body(request), "body");

            const auto items = body.list("steps", true, 1, rws::MotionQueue::MAX_JOBS);

            std::vector<rws::QueueJob> jobs;

            for (std::size_t i = 0; items && i < items->size(); ++i) {
                http::Fields step = body.item("steps", i, "RwsQueueStep");

                const auto name = step.string("name", true, 1, 64);
                const auto type = step.choice("type", {"move", "path", "home", "gripper"}, std::nullopt);
                const auto group = step.string("group", false, 1, 64);

                if (type) {
                    const rws::QueueJobType job_type =
                        *type == "move" ? rws::QueueJobType::move
                        : *type == "path" ? rws::QueueJobType::path
                        : *type == "home" ? rws::QueueJobType::home
                        : rws::QueueJobType::gripper;

                    http::Fields job_body = step.object("body", "RwsQueueJobBody", job_type != rws::QueueJobType::home);

                    ParsedJob parsed = parse_job(job_type, job_body, app.motion.default_speed());

                    step.merge(job_body);

                    if (step.ok()) {
                        parsed.job.name = *name;
                        parsed.job.group = group;

                        jobs.push_back(std::move(parsed.job));
                    }
                }

                body.merge(step);
            }

            body.check();

            rws::QueueSnapshot queue;

            try {
                queue = rws::call([&]() { return app.queue.start(identity.client_id, std::move(jobs)); });

            } catch (const std::invalid_argument& error) {
                throw http::validation_error({"body", "steps"}, error.what());
            }

            return Response::json(queue_json(queue), 202);
        }
    );

    // ------------------------------------------------------------
    // GET /robot/rws/queue (no lease)
    // ------------------------------------------------------------

    router.get(
        prefix + "/queue",
        [&app](Request& request) {
            app.auth.require_permission(request, "robot.read");

            return Response::json(queue_json(app.queue.snapshot()));
        }
    );

    // ------------------------------------------------------------
    // DELETE /robot/rws/queue: cancel a running queue (after the
    // jobs running now), clear a finished one. Whoever holds the
    // lease, not only the queue's owner, who may be gone.
    // ------------------------------------------------------------

    router.del(
        prefix + "/queue",
        [&app](Request& request) {
            app.auth.require_control(request, "robot.control");

            return Response::json(queue_json(app.queue.cancel_or_clear()));
        }
    );
}

}  // namespace gw
