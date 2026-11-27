#pragma once
#include <set>
#include "placement_common.hpp"

namespace lynx::placement {
struct CupResult {
    cv::Mat owners;
    std::set<int> consumed;
    json assignments = json::array();
};
CupResult assign_cups(const DepthImage& depth, const json& objects, const json& locations,
                      const cv::Matx33d& transform, const cv::Vec3d& translation,
                      const json& intrinsics, double cell_size, double table_z=0,
                      double check_height=160);
}
