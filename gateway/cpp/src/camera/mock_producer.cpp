#include "camera/mock_producer.hpp"

#include <chrono>

namespace gw {

http::Bytes MockCameraProducer::make_rgb() {
    http::Bytes data(3 * WIDTH * HEIGHT);

    std::size_t i = 0;

    //
    // Planar BGR: all blue, then all green, then all red.
    //
    for (int row = 0; row < HEIGHT; ++row) {
        for (int column = 0; column < WIDTH; ++column) {
            data[i++] = static_cast<std::uint8_t>(row * 255 / (HEIGHT - 1));
        }
    }

    for (int row = 0; row < HEIGHT; ++row) {
        for (int column = 0; column < WIDTH; ++column) {
            data[i++] = static_cast<std::uint8_t>(column * 255 / (WIDTH - 1));
        }
    }

    for (int pixel = 0; pixel < WIDTH * HEIGHT; ++pixel) {
        data[i++] = 128;
    }

    return data;
}

http::Bytes MockCameraProducer::make_depth() {
    http::Bytes data(2 * WIDTH * HEIGHT);

    std::size_t i = 0;

    //
    // uint16 little-endian millimetres, 0 in the left column.
    //
    for (int row = 0; row < HEIGHT; ++row) {
        for (int column = 0; column < WIDTH; ++column) {
            const int value = column == 0 ? 0 : 500 + row;

            data[i++] = static_cast<std::uint8_t>(value & 0xff);
            data[i++] = static_cast<std::uint8_t>(value >> 8);
        }
    }

    return data;
}

MockCameraProducer::MockCameraProducer(
    CameraService& camera
)
    : camera_(camera),
      rgb_(std::make_shared<const http::Bytes>(make_rgb())),
      depth_(std::make_shared<const http::Bytes>(make_depth())) {}

MockCameraProducer::~MockCameraProducer() {
    stop();
}

void MockCameraProducer::start() {
    {
        const std::lock_guard lock(mutex_);

        if (running_) {
            return;
        }

        running_ = true;
    }

    //
    // Placeholder values matching the 640x480 mock frames; they do
    // not describe a real camera.
    //
    CameraIntrinsics intrinsics;

    intrinsics.width = WIDTH;
    intrinsics.height = HEIGHT;
    intrinsics.fx = 500.0;
    intrinsics.fy = 500.0;
    intrinsics.cx = 320.0;
    intrinsics.cy = 240.0;
    intrinsics.matrix = {{
        {500.0, 0.0, 320.0},
        {0.0, 500.0, 240.0},
        {0.0, 0.0, 1.0},
    }};

    camera_.set_intrinsics(intrinsics);
    camera_.set_connected(true);

    thread_ = std::thread([this]() { run(); });
}

void MockCameraProducer::stop() {
    {
        const std::lock_guard lock(mutex_);

        if (!running_) {
            return;
        }

        running_ = false;
    }

    wake_.notify_all();

    if (thread_.joinable()) {
        thread_.join();
    }

    camera_.set_connected(false);
}

void MockCameraProducer::run() {
    std::unique_lock lock(mutex_);

    while (running_) {
        lock.unlock();

        camera_.publish_frame_set(
            FrameInput{rgb_, WIDTH, HEIGHT, "BGR888p"},
            FrameInput{depth_, WIDTH, HEIGHT, "RAW16"}
        );

        lock.lock();

        wake_.wait_for(lock, std::chrono::seconds(1), [this]() { return !running_; });
    }
}

}  // namespace gw
