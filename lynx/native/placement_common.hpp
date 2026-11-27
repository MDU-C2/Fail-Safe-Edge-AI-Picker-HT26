#pragma once
#include <opencv2/core.hpp>
#include "vendor/nlohmann/json.hpp"
#include "depth_calibration.hpp"

namespace lynx::placement {
using json = nlohmann::json;
// Shallow view: calibrate each requested sample without allocating another
// full depth image on the memory-limited native worker.
struct DepthImage {
    const cv::Mat raw;
    DepthCalibration calibration;
    int rows, cols, dims;
    DepthImage(const cv::Mat& image, DepthCalibration correction = {})
        : raw(image), calibration(correction), rows(image.rows), cols(image.cols), dims(image.dims) {}
    int type() const { return raw.type(); }
    int depth() const { return calibration.scale == 1. && calibration.offset == 0. ? raw.depth() : CV_64F; }
    bool empty() const { return raw.empty(); }
    cv::Size size() const { return raw.size(); }
    std::size_t total() const { return raw.total(); }
    DepthImage operator()(const cv::Rect& bounds) const { return DepthImage(raw(bounds), calibration); }
};
inline double depth_at(const DepthImage& depth, int y, int x) {
    double value;
    if (depth.type() == CV_16U) value = depth.raw.at<uint16_t>(y, x);
    else if (depth.type() == CV_32F) value = depth.raw.at<float>(y, x);
    else value = depth.raw.at<double>(y, x);
    return depth.calibration.correct(value);
}
inline double depth_at(const DepthImage& depth, cv::Point p) { return depth_at(depth, p.y, p.x); }
struct MaskRegion {
    int x = 0, y = 0;
    cv::Mat mask;
    bool valid() const { return !mask.empty(); }
};
MaskRegion decode_mask(const json& value, cv::Size image_size);
bool valid_box(const json& object, cv::Vec4d& box);
cv::Vec3d physical_point(double optical, int x, int y, const cv::Matx33d& transform,
                        const cv::Vec3d& translation, const json& intrinsics);
double rounded(double value, int places);
double scalar_rounded(double value, int places);
}
