#include "app.hpp"
#include "http/validate.hpp"
#include "rws/http_errors.hpp"
#include "rws/rapid.hpp"
#include "util/text.hpp"

namespace gw {

using http::HttpError;
using http::Json;
using http::Request;
using http::Response;

namespace {

constexpr std::size_t MAX_SOURCE_CHARACTERS = 1'000'000;

struct ModulePath {
    std::string task;
    std::string module;
};

//
// {task} and {module}: RAPID names, else 422.
//
ModulePath module_path(
    const Request& request
) {
    http::Fields path(
        Json{
            {"task", request.param("task")},
            {"module", request.param("module")},
        },
        "path"
    );

    const auto task = path.rapid_name("task", true);
    const auto module = path.rapid_name("module", true);

    path.check();

    return ModulePath{*task, *module};
}

Response status_response(
    App& app
) {
    return rws::call([&]() { return Response::json(rws::read_status(app.rws)); });
}

}  // namespace

void add_rapid_routes(
    App& app,
    http::Router& router
) {
    const std::string prefix = app.settings.api_prefix + "/robot/rapid";
    const std::string module_route = prefix + "/tasks/{task}/modules/{module}";

    // ------------------------------------------------------------
    // GET /robot/rapid
    // ------------------------------------------------------------

    router.get(
        prefix,
        [&app](Request& request) {
            app.auth.require_permission(request, "robot.read");

            return status_response(app);
        }
    );

    // ------------------------------------------------------------
    // PUT /robot/rapid/tasks/{task}/modules/{module}
    // ------------------------------------------------------------

    router.put(
        module_route,
        [&app](Request& request) {
            const Identity identity = app.auth.require_control(request, "robot.program");

            //
            // Path and body are validated before the controller is
            // contacted, then the source itself.
            //
            const ModulePath path = module_path(request);

            http::Fields body(http::object_body(request), "body");

            const auto source = body.string("source", true, 1, MAX_SOURCE_CHARACTERS);

            body.check();

            rws::check_source(path.module, *source);

            rws::call([&]() { rws::require_stopped_task(app.rws, path.task); });

            rws::call([&]() { app.rws.load_module(path.task, path.module, rws::to_latin1(*source).bytes); });

            app.events.publish(
                "robot.rapid.loaded",
                Json{
                    {"task", path.task},
                    {"module", path.module},
                    {"by", identity.client_id},
                }
            );

            return status_response(app);
        }
    );

    // ------------------------------------------------------------
    // DELETE /robot/rapid/tasks/{task}/modules/{module}
    // ------------------------------------------------------------

    router.del(
        module_route,
        [&app](Request& request) {
            const Identity identity = app.auth.require_control(request, "robot.program");

            const ModulePath path = module_path(request);

            rws::call([&]() { rws::require_stopped_task(app.rws, path.task); });

            const auto loaded = rws::call([&]() { return app.rws.program_modules(path.task); });

            bool found = false;

            for (const auto& name : loaded) {
                found = found || iequals(name, path.module);
            }

            if (!found) {
                throw HttpError(404, "Module " + path.module + " is not loaded in " + path.task);
            }

            rws::call([&]() { app.rws.unload_module(path.task, path.module); });

            app.events.publish(
                "robot.rapid.unloaded",
                Json{
                    {"task", path.task},
                    {"module", path.module},
                    {"by", identity.client_id},
                }
            );

            return status_response(app);
        }
    );

    // ------------------------------------------------------------
    // POST /robot/rapid/start
    // ------------------------------------------------------------

    router.post(
        prefix + "/start",
        [&app](Request& request) {
            const Identity identity = app.auth.require_control(request, "robot.control");

            //
            // The body is optional ({"reset_program_pointer": false}).
            //
            http::Fields body(http::object_body(request, true), "body");

            const bool reset = body.boolean("reset_program_pointer", false);

            body.check();

            rws::call([&]() { app.rws.start(reset); });

            app.events.publish(
                "robot.rapid.started",
                Json{
                    {"reset_program_pointer", reset},
                    {"by", identity.client_id},
                }
            );

            return status_response(app);
        }
    );

    // ------------------------------------------------------------
    // POST /robot/rapid/stop
    // ------------------------------------------------------------

    router.post(
        prefix + "/stop",
        [&app](Request& request) {
            const Identity identity = app.auth.require_control(request, "robot.control");

            rws::call([&]() { app.rws.stop(); });

            app.events.publish(
                "robot.rapid.stopped",
                Json{
                    {"by", identity.client_id},
                }
            );

            return status_response(app);
        }
    );
}

}  // namespace gw
