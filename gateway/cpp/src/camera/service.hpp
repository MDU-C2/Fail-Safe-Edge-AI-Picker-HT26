#pragma once

#include <array>
#include <cstdint>
#include <memory>
#include <mutex>
#include <optional>
#include <string>

#include "http/message.hpp"
#include "util/time.hpp"

namespace gw {

class EventBus;

// ================================================================
// Camera service
// ================================================================
//
// The latest RGB + depth frame set, as gateway/camera/service.py.
// Producers publish into it; any number of clients read it. Frame
// data is an immutable shared buffer: publishing replaces the
// pointer, readers keep the set they got alive while they send it,
// and nothing is copied.
//

struct CameraFrame {
    http::SharedBytes data;

    std::string content_type;

    std::uint64_t sequence = 0;

    WallTime timestamp;

    //
    // When it arrived, for its age (independent of wall-clock steps).
    //
    SteadyTime received;

    int width = 0;
    int height = 0;

    std::string pixel_format;
};

struct CameraFrameSet {
    std::uint64_t sequence = 0;

    WallTime timestamp;

    std::optional<CameraFrame> rgb;
    std::optional<CameraFrame> depth;
};

struct CameraIntrinsics {
    int width = 0;
    int height = 0;

    double fx = 0.0;
    double fy = 0.0;
    double cx = 0.0;
    double cy = 0.0;

    std::array<std::array<double, 3>, 3> matrix{};

    http::Json to_json() const;
};

struct FrameInput {
    http::SharedBytes data;

    int width = 0;
    int height = 0;

    std::string pixel_format;

    std::string content_type = "application/octet-stream";
};

class CameraService {
public:
    CameraService(
        EventBus& events,
        const Clock& clock = system_clock()
    );

    http::Json status_json() const;

    bool is_connected() const;

    std::optional<std::string> error() const;

    std::shared_ptr<const CameraFrameSet> latest() const;

    std::optional<CameraIntrinsics> intrinsics() const;

    void set_intrinsics(
        std::optional<CameraIntrinsics> intrinsics
    );

    //
    // Sets the connection state (error says why it is down) and
    // publishes camera.status when it changed.
    //
    void set_connected(
        bool connected,
        std::optional<std::string> error = std::nullopt
    );

    std::shared_ptr<const CameraFrameSet> publish_frame_set(
        std::optional<FrameInput> rgb,
        std::optional<FrameInput> depth,
        std::optional<WallTime> timestamp = std::nullopt
    );

    const Clock& clock() const {
        return clock_;
    }

private:
    http::Json status_json_locked() const;

    EventBus& events_;
    const Clock& clock_;

    mutable std::mutex mutex_;

    bool connected_ = false;
    std::optional<std::string> error_;

    std::uint64_t sequence_ = 0;

    std::shared_ptr<const CameraFrameSet> latest_;

    std::optional<CameraIntrinsics> intrinsics_;
};

// ================================================================
// Producers
// ================================================================

class CameraProducer {
public:
    virtual ~CameraProducer() = default;

    virtual void start() = 0;
    virtual void stop() = 0;
};

}  // namespace gw
