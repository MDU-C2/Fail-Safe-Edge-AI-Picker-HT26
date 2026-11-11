#include "app.hpp"
#include "util/text.hpp"

namespace gw {

using http::HttpError;
using http::Request;
using http::Response;

namespace {

//
// A frame, or 503 if there is none, the camera is disconnected or
// the frame is older than camera_max_frame_age_seconds.
//
Response frame_response(
    const App& app,
    const std::shared_ptr<const CameraFrameSet>& frame_set,
    const std::optional<CameraFrame>& frame,
    const char* name
) {
    if (!frame_set || !frame) {
        throw HttpError(503, format("No %s frame is currently available", name));
    }

    if (!app.camera.is_connected()) {
        throw HttpError(503, format("Camera is disconnected, no current %s frame", name));
    }

    const double age = to_seconds(app.camera.clock().steady() - frame->received);
    const double max_age = app.settings.camera_max_frame_age_seconds;

    if (age > max_age) {
        throw HttpError(
            503,
            format(
                "Latest %s frame is %.1f s old (limit %s s); the camera has stalled",
                name,
                age,
                format_g(max_age).c_str()
            )
        );
    }

    Response response;

    //
    // The frame set stays alive through shared_body until the
    // bytes are written; nothing is copied.
    //
    response.shared_body = frame->data;
    response.content_type = frame->content_type;

    response.headers = {
        {"X-Frame-Sequence", std::to_string(frame->sequence)},
        {"X-Frame-Timestamp", format_iso_time(frame->timestamp)},
        {"X-Frame-Width", std::to_string(frame->width)},
        {"X-Frame-Height", std::to_string(frame->height)},
        {"X-Pixel-Format", frame->pixel_format},
    };

    return response;
}

}  // namespace

void add_camera_routes(
    App& app,
    http::Router& router
) {
    const std::string prefix = app.settings.api_prefix + "/camera";

    router.get(
        prefix + "/status",
        [&app](Request& request) {
            app.auth.require_permission(request, "camera.read");

            return Response::json(app.camera.status_json());
        }
    );

    router.get(
        prefix + "/intrinsics",
        [&app](Request& request) {
            app.auth.require_permission(request, "camera.read");

            const auto intrinsics = app.camera.intrinsics();

            if (!intrinsics) {
                throw HttpError(503, "Camera intrinsics are not currently available");
            }

            return Response::json(intrinsics->to_json());
        }
    );

    router.get(
        prefix + "/rgb",
        [&app](Request& request) {
            app.auth.require_permission(request, "camera.read");

            const auto frame_set = app.camera.latest();

            return frame_response(app, frame_set, frame_set ? frame_set->rgb : std::nullopt, "RGB");
        }
    );

    router.get(
        prefix + "/depth",
        [&app](Request& request) {
            app.auth.require_permission(request, "camera.read");

            const auto frame_set = app.camera.latest();

            return frame_response(app, frame_set, frame_set ? frame_set->depth : std::nullopt, "depth");
        }
    );
}

}  // namespace gw
