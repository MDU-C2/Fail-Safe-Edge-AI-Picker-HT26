#include "camera/oak_producer.hpp"

#include <chrono>
#include <stdexcept>

#include "util/log.hpp"
#include "util/text.hpp"

namespace gw {

namespace {

using SteadyClock = std::chrono::steady_clock;

double seconds_since(
    SteadyClock::time_point time
) {
    return std::chrono::duration<double>(SteadyClock::now() - time).count();
}

}  // namespace

OakCameraProducer::OakCameraProducer(
    CameraService& camera,
    std::unique_ptr<OakConnector> connector,
    double fps,
    int width,
    int height,
    OakRecovery recovery
)
    : camera_(camera),
      connector_(std::move(connector)),
      fps_(fps),
      width_(width),
      height_(height),
      recovery_(recovery) {
    if (fps <= 0) {
        throw std::invalid_argument("fps must be greater than zero");
    }

    if (width <= 0 || height <= 0) {
        throw std::invalid_argument("Camera width and height must be greater than zero");
    }
}

OakCameraProducer::~OakCameraProducer() {
    stop();
}

bool OakCameraProducer::running() const {
    const std::lock_guard lock(mutex_);

    return running_;
}

// ================================================================
// Lifecycle
// ================================================================

void OakCameraProducer::start() {
    {
        const std::lock_guard lock(mutex_);

        if (running_) {
            return;
        }

        running_ = true;
    }

    try {
        device_ = connector_->open(recovery_.connect_timeout_seconds);

    } catch (...) {
        {
            const std::lock_guard lock(mutex_);

            running_ = false;
        }

        device_.reset();

        camera_.set_connected(false);

        throw;
    }

    camera_.set_intrinsics(device_->intrinsics());
    camera_.set_connected(true);

    thread_ = std::thread([this]() { capture_loop(); });
}

void OakCameraProducer::stop() {
    {
        const std::lock_guard lock(mutex_);

        if (!running_ && !thread_.joinable()) {
            return;
        }

        running_ = false;
    }

    wake_.notify_all();

    if (thread_.joinable()) {
        thread_.join();
    }

    device_.reset();

    camera_.set_connected(false);
}

void OakCameraProducer::wait(
    double seconds
) {
    std::unique_lock lock(mutex_);

    wake_.wait_for(lock, std::chrono::duration<double>(seconds), [this]() { return !running_; });
}

// ================================================================
// Capture
// ================================================================

void OakCameraProducer::capture_loop() {
    const double interval = 1.0 / fps_;

    auto last_frame = SteadyClock::now();

    while (running()) {
        const auto started = SteadyClock::now();

        try {
            std::optional<OakFrames> frames = device_->try_get();

            if (frames) {
                last_frame = started;

                //
                // RGB and depth have the same size: depth is aligned
                // to CAM_A at its output size.
                //
                camera_.publish_frame_set(
                    FrameInput{std::move(frames->rgb), width_, height_, "BGR888p", "application/x-oak-bgr888"},
                    FrameInput{std::move(frames->depth), width_, height_, "RAW16", "application/x-oak-depth"}
                );

            } else if (std::chrono::duration<double>(started - last_frame).count() > recovery_.stall_timeout_seconds) {
                throw std::runtime_error(format("no frames for %s s", format_g(recovery_.stall_timeout_seconds).c_str()));
            }

        } catch (const std::exception& error) {
            log::error(format("OAK camera capture error: %s", error.what()));

            reconnect(format("Capture failed: %s", error.what()));

            last_frame = SteadyClock::now();

            continue;
        }

        //
        // Keep the configured publication rate.
        //
        const double remaining = interval - seconds_since(started);

        if (remaining > 0) {
            wait(remaining);
        }
    }
}

void OakCameraProducer::reconnect(
    const std::string& reason
) {
    camera_.set_connected(false, reason);

    device_.reset();

    for (int attempt = 1; running(); ++attempt) {
        const double delay = attempt <= recovery_.quick_retries
            ? recovery_.quick_retry_seconds
            : recovery_.slow_retry_seconds;

        log::info(format("OAK camera: reconnect attempt %d in %s s", attempt, format_g(delay).c_str()));

        wait(delay);

        if (!running()) {
            return;
        }

        try {
            device_ = connector_->open(0.0);

        } catch (const std::exception& error) {
            device_.reset();

            camera_.set_connected(
                false,
                format("%s; reconnect attempt %d failed: %s", reason.c_str(), attempt, error.what())
            );

            continue;
        }

        camera_.set_intrinsics(device_->intrinsics());
        camera_.set_connected(true);

        log::info(format("OAK camera: reconnected after %d attempt(s)", attempt));

        return;
    }
}

}  // namespace gw
