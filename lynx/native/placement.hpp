#pragma once
#include <opencv2/core.hpp>
#include "vendor/nlohmann/json.hpp"

namespace lynx {
// Read-only assessment. Configuration and table reference are frozen by the
// coordinator; no fitting, placement-record mutation or robot I/O occurs here.
nlohmann::json assess_placement(const cv::Mat& depth, const nlohmann::json& objects,
                               const nlohmann::json& config);
}
