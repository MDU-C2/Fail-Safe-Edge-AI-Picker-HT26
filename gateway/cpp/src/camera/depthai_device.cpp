// ================================================================
// OAK-D Pro W PoE through depthai-core v3 (only built with depthai)
// ================================================================
//
// The pipeline of gateway/camera/oak_producer.py:
//
//   CAM_A  -> BGR888p at width x height ----------------+--> Sync "rgb"
//                                         inputAlignTo  |
//   CAM_B, CAM_C -> GRAY8 640x400 -> StereoDepth (ROBOTICS, LR check)
//                                         depth --------+--> Sync "depth"
//
// Sync pairs RGB and depth within 100 ms; the host queue keeps only
// the newest pair (size 1, non-blocking). Depth is aligned to CAM_A,
// so rgb[v, u] and depth[v, u] are the same point.
//

#include <chrono>
#include <thread>

#include <depthai/depthai.hpp>

#include "camera/oak_device.hpp"
#include "util/log.hpp"
#include "util/text.hpp"

namespace gw {

namespace {

class DepthaiDevice : public OakDevice {
public:
    DepthaiDevice(
        const OakConfig& config,
        double connect_timeout_seconds
    )
        : config_(config) {
        device_ = connect(connect_timeout_seconds);

        intrinsics_ = read_intrinsics();

        build_pipeline();

        pipeline_->start();

        if (config_.warmup_seconds > 0) {
            std::this_thread::sleep_for(std::chrono::duration<double>(config_.warmup_seconds));
        }
    }

    ~DepthaiDevice() override {
        queue_.reset();

        if (pipeline_) {
            try {
                pipeline_->stop();
            } catch (...) {
            }

            pipeline_.reset();
        }

        if (device_) {
            try {
                device_->close();
            } catch (...) {
            }

            device_.reset();
        }
    }

    CameraIntrinsics intrinsics() const override {
        return intrinsics_;
    }

    std::optional<OakFrames> try_get() override {
        const auto group = queue_->tryGet<dai::MessageGroup>();

        if (!group) {
            return std::nullopt;
        }

        const auto rgb = group->get<dai::ImgFrame>("rgb");
        const auto depth = group->get<dai::ImgFrame>("depth");

        if (!rgb || !depth) {
            return std::nullopt;
        }

        //
        // The frames own their buffers; the responses keep them
        // alive instead of copying the data out.
        //
        return OakFrames{share(rgb), share(depth)};
    }

private:
    static http::SharedBytes share(
        const std::shared_ptr<dai::ImgFrame>& frame
    ) {
        const auto data = frame->getData();

        return http::SharedBytes{frame, data.data(), data.size()};
    }

    //
    // PoE cameras may disappear for a moment while rebooting after a
    // pipeline closed, so connecting is retried until the timeout;
    // with 0 there is a single attempt.
    //
    std::shared_ptr<dai::Device> connect(
        double timeout_seconds
    ) {
        const auto deadline = std::chrono::steady_clock::now()
            + std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                std::chrono::duration<double>(timeout_seconds)
            );

        for (int attempt = 1;; ++attempt) {
            try {
                return std::make_shared<dai::Device>(dai::DeviceInfo(config_.ip));

            } catch (const std::runtime_error& error) {
                if (std::chrono::steady_clock::now() >= deadline) {
                    throw;
                }

                log::info(format("OAK camera %s not ready (attempt %d), retrying...", config_.ip.c_str(), attempt));

                std::this_thread::sleep_for(std::chrono::duration<double>(config_.reconnect_delay_seconds));
            }
        }
    }

    //
    // CAM_A intrinsics from the device calibration, converted to the
    // published size; depth is aligned to CAM_A, so they also apply
    // to the depth frames.
    //
    CameraIntrinsics read_intrinsics() {
        const auto matrix = device_->readCalibration().getCameraIntrinsics(
            dai::CameraBoardSocket::CAM_A,
            config_.width,
            config_.height
        );

        CameraIntrinsics intrinsics;

        intrinsics.width = config_.width;
        intrinsics.height = config_.height;

        for (int row = 0; row < 3; ++row) {
            for (int column = 0; column < 3; ++column) {
                intrinsics.matrix[row][column] = matrix.at(row).at(column);
            }
        }

        intrinsics.fx = intrinsics.matrix[0][0];
        intrinsics.fy = intrinsics.matrix[1][1];
        intrinsics.cx = intrinsics.matrix[0][2];
        intrinsics.cy = intrinsics.matrix[1][2];

        return intrinsics;
    }

    void build_pipeline() {
        pipeline_ = std::make_unique<dai::Pipeline>(device_);

        const auto fps = static_cast<float>(config_.fps);

        // --------------------------------------------------------
        // RGB camera
        // --------------------------------------------------------

        auto rgb_camera = pipeline_->create<dai::node::Camera>()->build(dai::CameraBoardSocket::CAM_A);

        auto* rgb_output = rgb_camera->requestOutput(
            std::make_pair<uint32_t, uint32_t>(config_.width, config_.height),
            dai::ImgFrame::Type::BGR888p,
            dai::ImgResizeMode::CROP,
            fps
        );

        // --------------------------------------------------------
        // Stereo cameras and depth
        // --------------------------------------------------------

        auto left_camera = pipeline_->create<dai::node::Camera>()->build(dai::CameraBoardSocket::CAM_B);
        auto right_camera = pipeline_->create<dai::node::Camera>()->build(dai::CameraBoardSocket::CAM_C);

        const auto stereo_size = std::make_pair<uint32_t, uint32_t>(config_.stereo_width, config_.stereo_height);

        auto* left_output = left_camera->requestOutput(stereo_size, dai::ImgFrame::Type::GRAY8, dai::ImgResizeMode::CROP, fps);
        auto* right_output = right_camera->requestOutput(stereo_size, dai::ImgFrame::Type::GRAY8, dai::ImgResizeMode::CROP, fps);

        auto stereo = pipeline_->create<dai::node::StereoDepth>();

        stereo->setDefaultProfilePreset(dai::node::StereoDepth::PresetMode::ROBOTICS);
        stereo->setLeftRightCheck(true);

        left_output->link(stereo->left);
        right_output->link(stereo->right);

        //
        // Align depth to the RGB output (how the DepthAI v3 example
        // aligns StereoDepth to the RGB stream on RVC2).
        //
        rgb_output->link(stereo->inputAlignTo);

        // --------------------------------------------------------
        // Synchronised RGB + depth to the host
        // --------------------------------------------------------

        auto sync = pipeline_->create<dai::node::Sync>();

        sync->setSyncThreshold(std::chrono::milliseconds(100));

        rgb_output->link(sync->inputs["rgb"]);
        stereo->depth.link(sync->inputs["depth"]);

        queue_ = sync->out.createOutputQueue(1, false);
    }

    OakConfig config_;

    std::shared_ptr<dai::Device> device_;
    std::unique_ptr<dai::Pipeline> pipeline_;
    std::shared_ptr<dai::MessageQueue> queue_;

    CameraIntrinsics intrinsics_;
};

class DepthaiConnector : public OakConnector {
public:
    explicit DepthaiConnector(
        OakConfig config
    )
        : config_(std::move(config)) {}

    std::unique_ptr<OakDevice> open(
        double connect_timeout_seconds
    ) override {
        return std::make_unique<DepthaiDevice>(config_, connect_timeout_seconds);
    }

private:
    OakConfig config_;
};

}  // namespace

std::unique_ptr<OakConnector> create_depthai_connector(
    const OakConfig& config
) {
    return std::make_unique<DepthaiConnector>(config);
}

}  // namespace gw
