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

//
// SharedVars.mod has some 60; a value is RAPID text such as
// [[1,2,3],[1,0,0,0],...] (a robtarget is ~150 characters).
//
constexpr std::size_t MAX_RESET_VARIABLES = 500;
constexpr std::size_t MAX_VALUE_CHARACTERS = 10'000;

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
    // GET /robot/rapid/tasks/{task}/modules/{module}
    // ------------------------------------------------------------
    //
    // The source as the controller saves it, also while RAPID runs:
    // a backup before the program is changed. PERS hold their
    // current values, not the declared ones.
    //

    router.get(
        module_route,
        [&app](Request& request) {
            app.auth.require_permission(request, "robot.read");

            const ModulePath path = module_path(request);

            const std::string name = rws::call([&]() {
                rws::require_task(app.rws, path.task);

                return rws::require_loaded_module(app.rws, path.task, path.module);
            });

            const std::string source = rws::call([&]() { return app.rws.module_source(path.task, name); });

            return Response::json(
                Json{
                    {"task", path.task},
                    {"module", name},
                    {"source", rws::latin1_to_utf8(source)},
                }
            );
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
    // POST /robot/rapid/reset
    // ------------------------------------------------------------
    //
    // Stops RAPID, writes the variables (the client sends the
    // values declared in rapid/SharedVars.mod: reloading a module
    // does not reset its PERS), reads them back, and starts RAPID
    // with the program pointer at main unless "start" is false.
    //

    router.post(
        prefix + "/reset",
        [&app](Request& request) {
            const Identity identity = app.auth.require_control(request, "robot.program");

            http::Fields body(http::object_body(request), "body");

            const auto task = body.rapid_name("task", true);

            const auto items = body.list("variables", true, 1, MAX_RESET_VARIABLES);

            std::vector<rws::RapidVariable> variables;

            bool watchdog = false;

            for (std::size_t i = 0; items && i < items->size(); ++i) {
                http::Fields fields = body.item("variables", i, "RapidVariable");

                const auto name = fields.rapid_name("name", true);
                const auto value = fields.string("value", true, 1, MAX_VALUE_CHARACTERS);

                if (fields.ok()) {
                    variables.push_back(rws::RapidVariable{*name, *value});

                    watchdog = watchdog || iequals(*name, "gw_session") || iequals(*name, "gw_timeout");
                }

                body.merge(fields);
            }

            const bool start = body.boolean("start", true);

            body.check();

            rws::call([&]() { rws::require_task(app.rws, *task); });

            rws::call([&]() { app.rws.stop(); });

            rws::call([&]() {
                //
                // Mastership also holds the client's lock, so the
                // heartbeat cannot write in between.
                //
                const rws::RwsClient::Mastership mastership(app.rws);

                rws::write_variables(app.rws, *task, variables);

                //
                // Before the start, or the arms would take the
                // gateway's session written later for a restart.
                //
                if (watchdog) {
                    app.heartbeat.restore();
                }
            });

            if (start) {
                rws::call([&]() { app.rws.start(true); });
            }

            Json names = Json::array();

            for (const auto& variable : variables) {
                names.push_back(variable.name);
            }

            app.events.publish(
                "robot.rapid.reset",
                Json{
                    {"task", *task},
                    {"variables", names},
                    {"started", start},
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
