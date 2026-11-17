#pragma once
#include <opencv2/core.hpp>
#include <set>
#include <vector>
#include <utility>
#include "vendor/nlohmann/json.hpp"

namespace lynx::placement {
using json = nlohmann::json;
struct SharedRegion {
    std::set<int> owners;
    std::vector<cv::Point> pixels;  // x = column, y = row; row-major order.
};
struct DepthResult {
    cv::Mat owners;  // CV_32S, same dimensions as depth.
    json assignments;
    std::vector<SharedRegion> shared;
};
// Depth is single-channel CV_16U, CV_32F, or CV_64F. Inputs are read-only by default.
// consume_owners permits reusing the ownership buffer when its values fit [-1,64];
// the caller must relinquish that input after the call. Other inputs stay read-only.
// Pixel attribution preserves exact-mask conflicts and keeps multi-cup ownership explicit.
DepthResult assign_depth_regions(const cv::Mat& depth, const json& objects,
    const json& locations, const cv::Mat& owners, const json& assignments,
    const cv::Matx33d& transform, const cv::Vec3d& translation,
    const json& intrinsics, double cell_size, double table_z = 0., double check_height = 160.,
    bool consume_owners = false);
std::pair<std::vector<SharedRegion>, json> cup_uncertainty_regions(
    const cv::Mat& depth, const json& objects, const json& locations, const json& assignments,
    const cv::Matx33d& transform, const cv::Vec3d& translation, const json& intrinsics,
    double cell_size, double table_z = 0., double check_height = 160.,
    double radius_mm = 40., double tail_radius_mm = 80.);
}
