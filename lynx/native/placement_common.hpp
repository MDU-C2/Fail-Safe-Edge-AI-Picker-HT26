#pragma once
#include <opencv2/core.hpp>
#include "vendor/nlohmann/json.hpp"

namespace lynx::placement {
using json = nlohmann::json;
// Promote each sample instead of retaining a second full float64 image.
inline double depth_at(const cv::Mat& depth, int y, int x) {
    if (depth.type() == CV_16U) return depth.at<uint16_t>(y, x);
    if (depth.type() == CV_32F) return depth.at<float>(y, x);
    return depth.at<double>(y, x);
}
inline double depth_at(const cv::Mat& depth, cv::Point p) { return depth_at(depth, p.y, p.x); }
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
