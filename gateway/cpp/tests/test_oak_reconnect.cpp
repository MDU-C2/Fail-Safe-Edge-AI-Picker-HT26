// Reconnect logic of the OAK producer, without a camera: DepthAI is
// replaced by a scripted fake. A port of gateway/tests/
// test_oak_reconnect.py; the real camera is test_oak.py (--oak).

#include <gtest/gtest.h>

#include <chrono>
#include <deque>
#include <functional>
#include <mutex>
#include <thread>
#include <variant>
#include <vector>

#include "camera/oak_producer.hpp"
#include "events/bus.hpp"

namespace gw {
namespace {

using Clock = std::chrono::steady_clock;

//
// What one try_get does: a frame, no frame yet, or a capture error.
//
struct Frame {};
struct NoFrame {};
struct Fail {
    std::string message;
};

using Step = std::variant<Frame, NoFrame, Fail>;

//
// The script shared by the connector and its devices: frames are
// played in turn (and keep coming when it runs out), the first
// failed_opens reconnects fail.
//
struct Script {
    std::mutex mutex;

    std::deque<Step> frames;
    int failed_opens = 0;

    std::vector<Clock::time_point> opens;
    int closes = 0;

    std::size_t open_count() {
        const std::lock_guard lock(mutex);

        return opens.size();
    }

    int close_count() {
        const std::lock_guard lock(mutex);

        return closes;
    }
};

class FakeDevice : public OakDevice {
public:
    explicit FakeDevice(
        Script& script
    )
        : script_(script) {}

    ~FakeDevice() override {
        const std::lock_guard lock(script_.mutex);

        ++script_.closes;
    }

    CameraIntrinsics intrinsics() const override {
        CameraIntrinsics intrinsics;

        intrinsics.width = 1;
        intrinsics.height = 1;
        intrinsics.fx = 1.0;
        intrinsics.fy = 1.0;
        intrinsics.matrix = {{{1, 0, 0}, {0, 1, 0}, {0, 0, 1}}};

        return intrinsics;
    }

    std::optional<OakFrames> try_get() override {
        Step step = Frame{};

        {
            const std::lock_guard lock(script_.mutex);

            if (!script_.frames.empty()) {
                step = script_.frames.front();
                script_.frames.pop_front();
            }
        }

        if (const auto* fail = std::get_if<Fail>(&step)) {
            throw std::runtime_error(fail->message);
        }

        if (std::holds_alternative<NoFrame>(step)) {
            return std::nullopt;
        }

        auto rgb = std::make_shared<const http::Bytes>(http::Bytes{'r', 'g', 'b'});
        auto depth = std::make_shared<const http::Bytes>(http::Bytes{'d', 'e', 'p', 't', 'h'});

        return OakFrames{http::SharedBytes::from(rgb), http::SharedBytes::from(depth)};
    }

private:
    Script& script_;
};

class FakeConnector : public OakConnector {
public:
    explicit FakeConnector(
        Script& script
    )
        : script_(script) {}

    std::unique_ptr<OakDevice> open(
        double
    ) override {
        {
            const std::lock_guard lock(script_.mutex);

            script_.opens.push_back(Clock::now());

            //
            // The first open is start(); only reconnects fail.
            //
            if (script_.opens.size() > 1 && script_.failed_opens > 0) {
                --script_.failed_opens;

                throw std::runtime_error("camera not found");
            }
        }

        return std::make_unique<FakeDevice>(script_);
    }

private:
    Script& script_;
};

OakRecovery fast_recovery() {
    OakRecovery recovery;

    recovery.stall_timeout_seconds = 0.3;
    recovery.quick_retries = 2;
    recovery.quick_retry_seconds = 0.05;
    recovery.slow_retry_seconds = 0.3;

    return recovery;
}

void wait_until(
    const std::function<bool()>& condition,
    double timeout_seconds = 5.0
) {
    const auto deadline = Clock::now() + std::chrono::duration_cast<Clock::duration>(std::chrono::duration<double>(timeout_seconds));

    while (!condition()) {
        ASSERT_LT(Clock::now(), deadline) << "timed out";

        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
}

double seconds(
    Clock::duration duration
) {
    return std::chrono::duration<double>(duration).count();
}

class OakReconnect : public ::testing::Test {
protected:
    std::unique_ptr<OakCameraProducer> make_producer(
        OakRecovery recovery = fast_recovery()
    ) {
        return std::make_unique<OakCameraProducer>(
            camera,
            std::make_unique<FakeConnector>(script),
            50.0,
            1,
            1,
            recovery
        );
    }

    std::uint64_t sequence() {
        const auto latest = camera.latest();

        return latest ? latest->sequence : 0;
    }

    EventBus events;
    CameraService camera{events};
    Script script;
};

TEST_F(OakReconnect, ReconnectsAfterCaptureErrorWithBackoff) {
    script.frames = {Frame{}, Frame{}, Fail{"X_LINK_ERROR"}};
    script.failed_opens = 3;

    auto oak = make_producer();

    oak->start();

    //
    // Down, with the reason, while it reconnects.
    //
    wait_until([&]() { return !camera.is_connected(); });

    const auto error = camera.error();

    ASSERT_TRUE(error.has_value());
    EXPECT_NE(error->find("X_LINK_ERROR"), std::string::npos);

    //
    // start, 3 failed reconnects, 1 good one.
    //
    wait_until([&]() { return script.open_count() == 5; });
    wait_until([&]() { return camera.is_connected(); });

    EXPECT_FALSE(camera.error().has_value());

    //
    // Two quick retries, then the slow interval.
    //
    {
        const std::lock_guard lock(script.mutex);

        EXPECT_LT(seconds(script.opens[2] - script.opens[1]), 0.2);
        EXPECT_GE(seconds(script.opens[3] - script.opens[2]), 0.25);
        EXPECT_GE(seconds(script.opens[4] - script.opens[3]), 0.25);
    }

    //
    // Frames flow again.
    //
    const auto before = sequence();

    wait_until([&]() { return sequence() > before; });

    oak->stop();
}

TEST_F(OakReconnect, ReconnectsWhenFramesStop) {
    //
    // One frame, then nothing for well over the stall timeout.
    //
    script.frames.push_back(Frame{});

    for (int i = 0; i < 1000; ++i) {
        script.frames.push_back(NoFrame{});
    }

    auto oak = make_producer();

    oak->start();

    wait_until([&]() { return script.open_count() == 2; });

    EXPECT_GE(script.close_count(), 1);

    oak->stop();
}

TEST_F(OakReconnect, StopInterruptsReconnectWait) {
    //
    // Every reconnect fails, so it ends up in the slow wait.
    //
    script.frames = {Fail{"gone"}};
    script.failed_opens = 1000;

    OakRecovery recovery = fast_recovery();

    recovery.slow_retry_seconds = 60.0;

    auto oak = make_producer(recovery);

    oak->start();

    //
    // start and the two quick retries; then the 60 s wait.
    //
    wait_until([&]() { return script.open_count() == 3; });

    std::this_thread::sleep_for(std::chrono::milliseconds(100));

    const auto started = Clock::now();

    oak->stop();

    EXPECT_LT(seconds(Clock::now() - started), 1.0);
    EXPECT_FALSE(camera.is_connected());
}

// ================================================================
// Not in the Python tests
// ================================================================

TEST_F(OakReconnect, FailedStartIsThrownAndLeavesItDisconnected) {
    class Refusing : public OakConnector {
    public:
        std::unique_ptr<OakDevice> open(double) override {
            throw std::runtime_error("Cannot find any device with given deviceInfo");
        }
    };

    OakCameraProducer oak(camera, std::make_unique<Refusing>(), 4.0, 1, 1);

    EXPECT_THROW(oak.start(), std::runtime_error);
    EXPECT_FALSE(camera.is_connected());

    //
    // Stopping after a failed start is harmless.
    //
    oak.stop();
}

TEST_F(OakReconnect, PublishesFramesWithTheOakFormats) {
    auto oak = make_producer();

    oak->start();

    wait_until([&]() { return sequence() > 0; });

    const auto latest = camera.latest();

    ASSERT_TRUE(latest->rgb && latest->depth);
    EXPECT_EQ(latest->rgb->pixel_format, "BGR888p");
    EXPECT_EQ(latest->rgb->content_type, "application/x-oak-bgr888");
    EXPECT_EQ(latest->depth->pixel_format, "RAW16");
    EXPECT_EQ(latest->depth->content_type, "application/x-oak-depth");
    EXPECT_EQ(std::string(reinterpret_cast<const char*>(latest->depth->data.data), latest->depth->data.size), "depth");

    EXPECT_TRUE(camera.intrinsics().has_value());

    oak->stop();
}

TEST_F(OakReconnect, RejectsBadSettings) {
    EXPECT_THROW(OakCameraProducer(camera, std::make_unique<FakeConnector>(script), 0.0, 1, 1), std::invalid_argument);
    EXPECT_THROW(OakCameraProducer(camera, std::make_unique<FakeConnector>(script), 4.0, 0, 1), std::invalid_argument);
}

}  // namespace
}  // namespace gw
