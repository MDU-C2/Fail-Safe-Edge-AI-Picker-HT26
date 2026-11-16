#pragma once

#include <condition_variable>
#include <memory>
#include <mutex>
#include <thread>

#include "camera/service.hpp"

namespace gw {

// ================================================================
// Mock camera
// ================================================================
//
// The test pattern of gateway/camera/mock_producer.py: 640x480
// BGR888p (planar; blue grows downwards, green to the right, red
// constant) and RAW16 depth (a plane from 500 mm at the top to
// 979 mm at the bottom, invalid left column), once a second. The
// same two buffers are published every time, so it costs ~1.5 MB.
//

class MockCameraProducer : public CameraProducer {
public:
    static constexpr int WIDTH = 640;
    static constexpr int HEIGHT = 480;

    explicit MockCameraProducer(
        CameraService& camera
    );

    ~MockCameraProducer() override;

    void start() override;
    void stop() override;

    static http::Bytes make_rgb();
    static http::Bytes make_depth();

private:
    void run();

    CameraService& camera_;

    std::shared_ptr<const http::Bytes> rgb_;
    std::shared_ptr<const http::Bytes> depth_;

    std::mutex mutex_;
    std::condition_variable wake_;

    bool running_ = false;

    std::thread thread_;
};

}  // namespace gw
