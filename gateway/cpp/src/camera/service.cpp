#include "camera/service.hpp"

#include "events/bus.hpp"

namespace gw {

using http::Json;

Json CameraIntrinsics::to_json() const {
    Json rows = Json::array();

    for (const auto& row : matrix) {
        rows.push_back(Json::array({row[0], row[1], row[2]}));
    }

    return Json{
        {"width", width},
        {"height", height},
        {"fx", fx},
        {"fy", fy},
        {"cx", cx},
        {"cy", cy},
        {"matrix", rows},
    };
}

CameraService::CameraService(
    EventBus& events,
    const Clock& clock
)
    : events_(events),
      clock_(clock) {}

Json CameraService::status_json_locked() const {
    const CameraFrameSet* latest = latest_.get();

    const CameraFrame* rgb = latest != nullptr && latest->rgb ? &*latest->rgb : nullptr;
    const CameraFrame* depth = latest != nullptr && latest->depth ? &*latest->depth : nullptr;

    auto value_or_null = [](bool present, auto value) -> Json {
        return present ? Json(value) : Json(nullptr);
    };

    return Json{
        {"connected", connected_},
        {"rgb_available", rgb != nullptr},
        {"depth_available", depth != nullptr},
        {"sequence", value_or_null(latest != nullptr, latest != nullptr ? latest->sequence : 0)},
        {"timestamp", value_or_null(latest != nullptr, latest != nullptr ? format_json_time(latest->timestamp) : "")},
        {"rgb_width", value_or_null(rgb != nullptr, rgb != nullptr ? rgb->width : 0)},
        {"rgb_height", value_or_null(rgb != nullptr, rgb != nullptr ? rgb->height : 0)},
        {"rgb_format", value_or_null(rgb != nullptr, rgb != nullptr ? rgb->pixel_format : "")},
        {"depth_width", value_or_null(depth != nullptr, depth != nullptr ? depth->width : 0)},
        {"depth_height", value_or_null(depth != nullptr, depth != nullptr ? depth->height : 0)},
        {"depth_format", value_or_null(depth != nullptr, depth != nullptr ? depth->pixel_format : "")},
        {"error", error_ ? Json(*error_) : Json(nullptr)},
    };
}

Json CameraService::status_json() const {
    const std::lock_guard lock(mutex_);

    return status_json_locked();
}

bool CameraService::is_connected() const {
    const std::lock_guard lock(mutex_);

    return connected_;
}

std::optional<std::string> CameraService::error() const {
    const std::lock_guard lock(mutex_);

    return error_;
}

std::shared_ptr<const CameraFrameSet> CameraService::latest() const {
    const std::lock_guard lock(mutex_);

    return latest_;
}

std::optional<CameraIntrinsics> CameraService::intrinsics() const {
    const std::lock_guard lock(mutex_);

    return intrinsics_;
}

void CameraService::set_intrinsics(
    std::optional<CameraIntrinsics> intrinsics
) {
    const std::lock_guard lock(mutex_);

    intrinsics_ = std::move(intrinsics);
}

void CameraService::set_connected(
    bool connected,
    std::optional<std::string> error
) {
    if (connected) {
        error.reset();
    }

    Json status;

    {
        const std::lock_guard lock(mutex_);

        if (connected_ == connected && error_ == error) {
            return;
        }

        connected_ = connected;
        error_ = std::move(error);

        status = status_json_locked();
    }

    events_.publish("camera.status", status);
}

std::shared_ptr<const CameraFrameSet> CameraService::publish_frame_set(
    std::optional<FrameInput> rgb,
    std::optional<FrameInput> depth,
    std::optional<WallTime> timestamp
) {
    const WallTime when = timestamp.value_or(clock_.wall());
    const SteadyTime received = clock_.steady();

    const std::lock_guard lock(mutex_);

    auto frame_set = std::make_shared<CameraFrameSet>();

    frame_set->sequence = ++sequence_;
    frame_set->timestamp = when;

    auto make_frame = [&](FrameInput& input) {
        return CameraFrame{
            std::move(input.data),
            std::move(input.content_type),
            frame_set->sequence,
            when,
            received,
            input.width,
            input.height,
            std::move(input.pixel_format),
        };
    };

    if (rgb) {
        frame_set->rgb = make_frame(*rgb);
    }

    if (depth) {
        frame_set->depth = make_frame(*depth);
    }

    latest_ = frame_set;

    connected_ = true;
    error_.reset();

    return latest_;
}

}  // namespace gw
