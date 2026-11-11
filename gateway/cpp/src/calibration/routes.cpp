#include "app.hpp"
#include "util/text.hpp"

namespace gw {

using http::HttpError;
using http::Json;
using http::Request;
using http::Response;

namespace {

constexpr std::size_t MAX_CALIBRATION_BYTES = 1'000'000;

}  // namespace

void add_calibration_routes(
    App& app,
    http::Router& router
) {
    const std::string path = app.settings.api_prefix + "/calibration";

    // ------------------------------------------------------------
    // GET /calibration
    // ------------------------------------------------------------

    router.get(
        path,
        [&app](Request& request) {
            app.auth.require_permission(request, "camera.read");

            const auto stored = app.calibration.get();

            if (!stored) {
                throw HttpError(404, "No calibration has been uploaded");
            }

            return Response::json(stored->to_json());
        }
    );

    // ------------------------------------------------------------
    // POST /calibration
    // ------------------------------------------------------------

    router.post(
        path,
        [&app](Request& request) {
            //
            // As in FastAPI, the dependencies (permission, lease)
            // are checked before the body.
            //
            const Identity identity = app.auth.require_control(request, "robot.control");

            if (request.body.empty()) {
                throw http::validation_error({"body"}, "Field required", "missing");
            }

            Json calibration = http::parse_json_body(request);

            if (!calibration.is_object()) {
                throw http::validation_error(
                    {"body"},
                    "Input should be a valid dictionary",
                    "dict_type",
                    calibration
                );
            }

            if (python_json_length(calibration) > MAX_CALIBRATION_BYTES) {
                throw HttpError(
                    413,
                    format("Calibration is larger than %zu bytes", MAX_CALIBRATION_BYTES)
                );
            }

            const StoredCalibration stored = app.calibration.set(std::move(calibration), identity.client_id);

            app.events.publish(
                "calibration.updated",
                Json{
                    {"updated_at", format_iso_time(stored.updated_at)},
                    {"updated_by", stored.updated_by},
                }
            );

            return Response::json(stored.to_json());
        }
    );
}

}  // namespace gw
