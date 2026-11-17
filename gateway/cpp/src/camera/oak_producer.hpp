#pragma once

#include <condition_variable>
#include <memory>
#include <mutex>
#include <string>
#include <thread>

#include "camera/oak_device.hpp"
#include "camera/service.hpp"

namespace gw {

// ================================================================
// OAK camera producer
// ================================================================
//
// gateway/camera/oak_producer.py: opens the camera at start (a
// failure stops the gateway from starting), then publishes the
// newest RGB + depth pair at the configured rate from its own
// thread. If capture fails, or no frame arrives for
// stall_timeout_seconds, the camera is closed and reopened:
// quick_retries attempts quick_retry_seconds apart, then one every
// slow_retry_seconds until it is back or the producer is stopped.
// Meanwhile the camera service reports it disconnected, with the
// reason, and the frame routes answer 503.
//

struct OakRecovery {
    double connect_timeout_seconds = 60.0;

    double stall_timeout_seconds = 10.0;

    int quick_retries = 5;
    double quick_retry_seconds = 5.0;
    double slow_retry_seconds = 60.0;
};

class OakCameraProducer : public CameraProducer {
public:
    OakCameraProducer(
        CameraService& camera,
        std::unique_ptr<OakConnector> connector,
        double fps,
        int width,
        int height,
        OakRecovery recovery = {}
    );

    ~OakCameraProducer() override;

    void start() override;
    void stop() override;

private:
    void capture_loop();

    void reconnect(
        const std::string& reason
    );

    //
    // Sleeps, but returns at once when stop() is called.
    //
    void wait(
        double seconds
    );

    bool running() const;

    CameraService& camera_;

    std::unique_ptr<OakConnector> connector_;

    double fps_;
    int width_;
    int height_;

    OakRecovery recovery_;

    //
    // Only touched by start(), the capture thread and stop() after
    // joining it.
    //
    std::unique_ptr<OakDevice> device_;

    mutable std::mutex mutex_;
    std::condition_variable wake_;

    bool running_ = false;

    std::thread thread_;
};

}  // namespace gw
