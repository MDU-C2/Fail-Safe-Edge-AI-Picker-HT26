#pragma once

#include <opencv2/core.hpp>
#include <vector>
#include "vendor/nlohmann/json.hpp"

namespace lynx {

// Pure perception: no camera, robot, filesystem, or shared mutable state.
// detections is the native response document; masks is its cropped uint8 payload.
// depths contains 1..9 aligned CV_16UC1 frames, oldest first and newest last.
// One frame omits temporal telemetry; multiple frames average available positive mask medians.
// config contains calibration{scale,R,t,tag_offset}, intrinsics{fx,fy,cx,cy}, names.
// Extra config fields (grid/reference identity, etc.) are deliberately ignored.
// Returns the Python Detector object's JSON array, including exact packed masks.
// Retained object crops are limited to 262144 pixels each and 8388608 in total.
// Malformed input throws a validation/JSON exception; unavailable depth is null.
nlohmann::json describe_objects(const nlohmann::json& detections,
                               const std::vector<unsigned char>& masks,
                               const std::vector<cv::Mat>& depths,
                               const nlohmann::json& config);

}  // namespace lynx
