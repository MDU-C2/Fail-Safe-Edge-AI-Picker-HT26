#pragma once

#include <memory>
#include <optional>
#include <string>

#include "camera/service.hpp"
#include "http/message.hpp"

namespace gw {

// ================================================================
// OAK device interface
// ================================================================
//
// What OakCameraProducer needs from the camera: open it (device,
// pipeline, warm-up) and read the newest RGB + depth pair. The real
// one is DepthAI (depthai_device.cpp, only built with depthai-core);
// the unit tests use a scripted fake. Closing is destroying the
// device. Errors are std::exception.
//

struct OakFrames {
    //
    // BGR888p (planar) and RAW16 depth in mm aligned to the RGB
    // camera, both of the configured size; owned by the driver's
    // frames, never copied.
    //
    http::SharedBytes rgb;
    http::SharedBytes depth;
};

struct OakConfig {
    std::string ip = "169.254.1.223";

    double fps = 4.0;

    int width = 1920;
    int height = 1080;

    int stereo_width = 640;
    int stereo_height = 400;

    //
    // After starting the pipeline, before reading frames: auto
    // exposure, white balance and stereo settle.
    //
    double warmup_seconds = 2.0;

    //
    // Between connection attempts while opening.
    //
    double reconnect_delay_seconds = 3.0;
};

class OakDevice {
public:
    virtual ~OakDevice() = default;

    //
    // CAM_A intrinsics, scaled to the published frame size.
    //
    virtual CameraIntrinsics intrinsics() const = 0;

    //
    // The newest frame pair, or nothing if none is ready; throws
    // when capture fails.
    //
    virtual std::optional<OakFrames> try_get() = 0;
};

class OakConnector {
public:
    virtual ~OakConnector() = default;

    //
    // Connects (retrying until connect_timeout_seconds; 0 = one
    // attempt), builds and starts the pipeline. Throws on failure.
    //
    virtual std::unique_ptr<OakDevice> open(
        double connect_timeout_seconds
    ) = 0;
};

//
// The DepthAI connector, or null when the gateway was built without
// depthai-core.
//
std::unique_ptr<OakConnector> create_depthai_connector(
    const OakConfig& config
);

}  // namespace gw
