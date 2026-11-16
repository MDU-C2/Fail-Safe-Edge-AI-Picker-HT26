#include "object_depth.hpp"

#include <opencv2/imgproc.hpp>
#include <algorithm>
#include <array>
#include <charconv>
#include <cmath>
#include <cstdint>
#include <limits>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>
#include <system_error>

namespace lynx {
namespace {
using json = nlohmann::json;
constexpr int kMaxWidth = 1920, kMaxHeight = 1080;
constexpr std::size_t kMaxDetections = 64, kMaxMaskBytes = 64 * 1024 * 1024;
constexpr std::size_t kMaxObjectCropPixels = 262144, kMaxRetainedCropPixels = 8388608;
constexpr int kEdgePixels = 5, kMinCorePixels = 9;
constexpr double kValidFraction = 0.6, kCupZMin = -30.0, kCupZMax = 150.0;

[[noreturn]] void invalid(const char* message) { throw std::invalid_argument(message); }

double number(const json& value) {
    if (!value.is_number() || value.is_boolean()) invalid("Expected a finite numeric value");
    const double result = value.get<double>();
    if (!std::isfinite(result)) invalid("Expected a finite numeric value");
    return result;
}

int integer(const json& value, int low, int high) {
    if (!value.is_number_integer() || value.is_boolean()) invalid("Expected bounded integer");
    if (value.is_number_unsigned()) {
        const auto result = value.get<std::uint64_t>();
        if (result > static_cast<std::uint64_t>(high)) invalid("Integer exceeds limit");
        if (result < static_cast<std::uint64_t>(std::max(0, low))) invalid("Integer below limit");
        return static_cast<int>(result);
    }
    const auto result = value.get<std::int64_t>();
    if (result < low || result > high) invalid("Integer outside limits");
    return static_cast<int>(result);
}

// Python round(float, digits) rounds the exact binary value to decimal places.
// nearbyint(value*10) is NOT equivalent (e.g. Python round(1.15,1) is 1.1).
// Fixed to_chars uses nearest decimal rounding without locale or fenv mutation.
double rounded(double value, int digits) {
    if (!std::isfinite(value)) invalid("Coordinate calculation is not finite");
    std::array<char, 512> buffer{};
    const auto formatted = std::to_chars(buffer.data(), buffer.data() + buffer.size(),
                                         value, std::chars_format::fixed, digits);
    if (formatted.ec != std::errc{}) invalid("Coordinate rounding failed");
    double result = 0;
    const auto parsed = std::from_chars(buffer.data(), formatted.ptr, result);
    if (parsed.ec != std::errc{} || parsed.ptr != formatted.ptr || !std::isfinite(result))
        invalid("Coordinate rounding failed");
    return result;
}

json measurement(const std::optional<double>& value) {
    return value ? json(rounded(*value, 1)) : json(nullptr);
}

struct CameraGeometry {
    double fx, fy, cx, cy;
    cv::Matx33d scaled_rotation;
    cv::Vec3d translation;
    cv::Vec3d robot(double u, double v, double depth) const {
        const cv::Vec3d point((u-cx)*depth/fx, (v-cy)*depth/fy, depth);
        const cv::Vec3d result = scaled_rotation * point + translation;
        for (int i = 0; i < 3; ++i)
            if (!std::isfinite(result[i])) invalid("Robot coordinate is not finite");
        return result;
    }
};

CameraGeometry geometry(const json& config, int width, int height) {
    if (!config.is_object() || !config.contains("intrinsics") || !config.contains("calibration"))
        invalid("Missing camera geometry");
    const auto& intr = config.at("intrinsics");
    const auto& cal = config.at("calibration");
    CameraGeometry result{};
    result.fx = number(intr.at("fx")); result.fy = number(intr.at("fy"));
    result.cx = number(intr.at("cx")); result.cy = number(intr.at("cy"));
    if (result.fx <= 0 || result.fy <= 0) invalid("Focal lengths must be positive");
    if (intr.contains("width") && integer(intr.at("width"), 1, kMaxWidth) != width)
        invalid("Intrinsics width disagrees with frame");
    if (intr.contains("height") && integer(intr.at("height"), 1, kMaxHeight) != height)
        invalid("Intrinsics height disagrees with frame");
    const double scale = number(cal.at("scale"));
    if (scale <= 0) invalid("Calibration scale must be positive");
    const auto& rotation = cal.at("R"); const auto& translation = cal.at("t");
    if (!rotation.is_array() || rotation.size() != 3 || !translation.is_array() || translation.size() != 3)
        invalid("Invalid calibration dimensions");
    for (int row = 0; row < 3; ++row) {
        if (!rotation[row].is_array() || rotation[row].size() != 3) invalid("Invalid rotation dimensions");
        result.translation[row] = number(translation[row]);
        for (int col = 0; col < 3; ++col) {
            result.scaled_rotation(row, col) = scale * number(rotation[row][col]);
            if (!std::isfinite(result.scaled_rotation(row, col))) invalid("Invalid scaled rotation");
        }
    }
    if (cal.contains("tag_offset")) {
        const auto& offset = cal.at("tag_offset");
        if (!offset.is_array() || offset.size() != 3) invalid("Invalid tag offset");
        for (const auto& part : offset) (void)number(part);
    }
    return result;  // The planner, not Detector.describe, applies the tag offset.
}

std::optional<double> median(std::vector<std::uint16_t>& samples) {
    if (samples.empty()) return std::nullopt;
    const auto middle = samples.begin() + static_cast<std::ptrdiff_t>(samples.size()/2);
    std::nth_element(samples.begin(), middle, samples.end());
    const double upper = *middle;
    if (samples.size()%2) return upper;
    return (upper + *std::max_element(samples.begin(), middle))/2.0;
}

using PixelIndices = std::vector<std::uint32_t>;

std::optional<double> values_at(const cv::Mat& depth, const cv::Rect& bounds, const PixelIndices& points,
                                std::size_t* valid_count = nullptr) {
    std::vector<std::uint16_t> values;
    values.reserve(points.size());
    for (const auto index : points) {
        const auto value = depth.at<std::uint16_t>(bounds.y+index/bounds.width, bounds.x+index%bounds.width);
        if (value) values.push_back(value);
    }
    if (valid_count) *valid_count = values.size();
    return median(values);
}

struct MaskGeometry {
    cv::Rect bounds;
    cv::Mat crop;
    // Crop-local uint32 indices avoid two full-image cv::Point vectors when a
    // large mask is returned. Each index is bounded by 1920*1080.
    PixelIndices pixels, inner, core, center;
    double u = 0, v = 0;
    int center_size = 0;
    const char* core_failure = nullptr;
};

MaskGeometry mask_geometry(const cv::Mat& raw, int origin_x, int origin_y,
                           int width, int height, bool base) {
    MaskGeometry result;
    const auto local = cv::boundingRect(raw);
    if (local.empty()) invalid("Native mask is empty");
    result.bounds = cv::Rect(origin_x+local.x, origin_y+local.y, local.width, local.height);
    result.crop = raw(local);  // View of immutable caller payload; never retained in output.
    result.pixels.reserve(static_cast<std::size_t>(cv::countNonZero(result.crop)));
    std::uint64_t sx = 0, sy = 0;
    for (int y = 0; y < result.crop.rows; ++y) {
        const auto* row = result.crop.ptr<unsigned char>(y);
        for (int x = 0; x < result.crop.cols; ++x) if (row[x]) {
            const cv::Point point(x+result.bounds.x, y+result.bounds.y);
            result.pixels.push_back(static_cast<std::uint32_t>(y*result.crop.cols+x));
            sx += point.x; sy += point.y;
        }
    }
    result.u = static_cast<double>(sx)/result.pixels.size();
    result.v = static_cast<double>(sy)/result.pixels.size();
    if (!base) {
        // Five explicit background pixels reproduce full-frame erosion. At a
        // genuine image edge, clipping preserves OpenCV's high exterior border.
        const cv::Rect frame(0, 0, width, height);
        const cv::Rect expanded = (result.bounds + cv::Size(2*kEdgePixels, 2*kEdgePixels)
                                   - cv::Point(kEdgePixels, kEdgePixels)) & frame;
        cv::Mat padded(expanded.height, expanded.width, CV_8U, cv::Scalar(0));
        result.crop.copyTo(padded(cv::Rect(result.bounds.x-expanded.x, result.bounds.y-expanded.y,
                                           result.bounds.width, result.bounds.height)));
        cv::Mat shrunk;
        cv::erode(padded, shrunk, cv::Mat::ones(2*kEdgePixels+1, 2*kEdgePixels+1, CV_8U));
        result.inner.reserve(result.pixels.size());
        for (const auto index : result.pixels) {
            const int x = result.bounds.x+index%result.bounds.width;
            const int y = result.bounds.y+index/result.bounds.width;
            if (shrunk.at<unsigned char>(y-expanded.y, x-expanded.x)) result.inner.push_back(index);
        }
        if (result.inner.empty()) result.inner = result.pixels;
        return result;
    }
    if (result.pixels.size() < kMinCorePixels) {
        result.core_failure = "CUP BASE mask is too small for reliable interior depth.";
        return result;
    }
    cv::Mat padded, distances;
    cv::copyMakeBorder(result.crop, padded, 1, 1, 1, 1, cv::BORDER_CONSTANT, cv::Scalar(0));
    cv::distanceTransform(padded, distances, cv::DIST_L2, 5);
    double maximum = 0;
    cv::minMaxLoc(distances, nullptr, &maximum);
    const double minimum = std::max(2.0, maximum*0.5);
    const int cx = static_cast<int>(std::floor(result.u+0.5));
    const int cy = static_cast<int>(std::floor(result.v+0.5));
    const int lx = cx-result.bounds.x, ly = cy-result.bounds.y;
    if (!result.crop.at<unsigned char>(ly, lx) || distances.at<float>(ly+1, lx+1) < minimum) {
        result.core_failure = "CUP BASE centre is not inside a sufficiently thick mask interior.";
        return result;
    }
    const double half_width = std::max(1.5, result.bounds.width/4.0);
    const double half_height = std::max(1.5, result.bounds.height/4.0);
    for (const auto index : result.pixels) {
        const int x = index%result.bounds.width, y = index/result.bounds.width;
        if (std::abs(x+result.bounds.x-result.u) <= half_width && std::abs(y+result.bounds.y-result.v) <= half_height &&
            distances.at<float>(y+1, x+1) >= minimum)
            result.core.push_back(index);
    }
    for (int y = std::max(0, cy-1); y < std::min(height, cy+2); ++y)
        for (int x = std::max(0, cx-1); x < std::min(width, cx+2); ++x) {
            ++result.center_size;
            if (result.bounds.contains(cv::Point(x, y)) &&
                result.crop.at<unsigned char>(y-result.bounds.y, x-result.bounds.x))
                result.center.push_back(static_cast<std::uint32_t>((y-result.bounds.y)*result.bounds.width+x-result.bounds.x));
        }
    return result;
}

struct DepthValue { std::optional<double> value; json quality; };

DepthValue core_depth(const MaskGeometry& mask, const cv::Mat& depth) {
    std::size_t valid_mask = 0;
    for (const auto index : mask.pixels)
        valid_mask += depth.at<std::uint16_t>(mask.bounds.y+index/mask.bounds.width, mask.bounds.x+index%mask.bounds.width) > 0;
    json quality = {{"method", "interior_median"}, {"valid", false},
                    {"mask_pixels", mask.pixels.size()}, {"valid_mask_pixels", valid_mask},
                    {"core_pixels", 0}, {"valid_core_pixels", 0}, {"core_valid_fraction", 0.0},
                    {"center_pixels", 0}, {"valid_center_pixels", 0}};
    auto rejected = [&](const char* reason) -> DepthValue {
        quality["reason"] = reason;
        return {std::nullopt, quality};
    };
    if (mask.core_failure) return rejected(mask.core_failure);
    std::size_t valid_core = 0, valid_center = 0;
    const auto value = values_at(depth, mask.bounds, mask.core, &valid_core);
    for (const auto index : mask.center)
        valid_center += depth.at<std::uint16_t>(mask.bounds.y+index/mask.bounds.width, mask.bounds.x+index%mask.bounds.width) > 0;
    quality["core_pixels"] = mask.core.size(); quality["valid_core_pixels"] = valid_core;
    quality["core_valid_fraction"] = mask.core.empty() ? 0.0 : rounded(static_cast<double>(valid_core)/mask.core.size(), 4);
    quality["center_pixels"] = mask.center.size(); quality["valid_center_pixels"] = valid_center;
    if (mask.core.size() < kMinCorePixels || mask.center_size != 9 || mask.center.size() != 9)
        return rejected("CUP BASE interior is too small for reliable depth.");
    if (valid_center < static_cast<std::size_t>(std::ceil(9*kValidFraction)))
        return rejected("CUP BASE has insufficient valid depth at its centre.");
    if (valid_core < static_cast<std::size_t>(std::ceil(mask.core.size()*kValidFraction)))
        return rejected("CUP BASE has insufficient valid depth across its interior.");
    quality["valid"] = true;
    quality["reason"] = "Finite positive depth covers the centre and interior.";
    return {value, quality};
}

std::string base64(const std::vector<unsigned char>& bytes) {
    constexpr char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    std::string result;
    result.reserve((bytes.size()+2)/3*4);
    for (std::size_t i = 0; i < bytes.size(); i += 3) {
        const std::uint32_t value = (std::uint32_t(bytes[i]) << 16) |
            (i+1 < bytes.size() ? std::uint32_t(bytes[i+1]) << 8 : 0) |
            (i+2 < bytes.size() ? std::uint32_t(bytes[i+2]) : 0);
        result += alphabet[(value >> 18)&63]; result += alphabet[(value >> 12)&63];
        result += i+1 < bytes.size() ? alphabet[(value >> 6)&63] : '=';
        result += i+2 < bytes.size() ? alphabet[value&63] : '=';
    }
    return result;
}

json segmentation(const MaskGeometry& mask) {
    const std::size_t count = static_cast<std::size_t>(mask.crop.rows)*mask.crop.cols;
    std::vector<unsigned char> packed((count+7)/8, 0);
    std::size_t index = 0;
    for (int y = 0; y < mask.crop.rows; ++y)
        for (int x = 0; x < mask.crop.cols; ++x, ++index)
            if (mask.crop.at<unsigned char>(y, x)) packed[index/8] |= static_cast<unsigned char>(1u << (7-index%8));
    return {{"encoding", "base64-packbits-big"}, {"origin", {mask.bounds.x, mask.bounds.y}},
            {"size", {mask.bounds.height, mask.bounds.width}}, {"data", base64(packed)}};
}

json describe(const std::string& name, double confidence, const MaskGeometry& mask,
              const std::vector<cv::Mat>& depths, const CameraGeometry& camera) {
    const bool is_base = name == "CUP BASE";
    auto latest = is_base ? core_depth(mask, depths.back())
                          : DepthValue{values_at(depths.back(), mask.bounds, mask.inner), nullptr};
    auto value = latest.value;
    json frames = json::array(), robot_frames = json::array(), frame_quality = json::array();
    if (depths.size() > 1) {
        double low = std::numeric_limits<double>::infinity(), high = -low, sum = 0;
        bool stable = true, all_valid = true;
        for (const auto& depth : depths) {
            const auto current = is_base ? core_depth(mask, depth)
                                        : DepthValue{values_at(depth, mask.bounds, mask.inner), nullptr};
            frames.push_back(measurement(current.value));
            if (current.value) {
                low = std::min(low, *current.value); high = std::max(high, *current.value);
                sum += *current.value;
                robot_frames.push_back(rounded(camera.robot(mask.u, mask.v, *current.value)[2], 1));
            } else { stable = false; robot_frames.push_back(nullptr); }
            if (is_base) { frame_quality.push_back(current.quality); all_valid &= current.quality.at("valid").get<bool>(); }
        }
        stable = stable && high-low <= 20.0;
        value = stable ? std::optional<double>(sum/depths.size()) : std::nullopt;
        if (is_base) {
            latest.quality["method"] = "temporal_interior_mean";
            latest.quality["sample_count"] = depths.size(); latest.quality["frame_quality"] = frame_quality;
            latest.quality["valid"] = stable && all_valid;
            latest.quality["reason"] = stable && all_valid
                ? "All frame cores have stable positive depth."
                : "CUP BASE depth is missing or varies by more than 20 mm across frames.";
            if (!(stable && all_valid)) value = std::nullopt;
        }
    }
    json result = {{"class", name}, {"confidence", rounded(confidence, 3)},
        {"x_px", rounded(mask.u, 1)}, {"y_px", rounded(mask.v, 1)},
        {"x_mm", nullptr}, {"y_mm", nullptr}, {"z_mm", measurement(value)},
        {"robot_x_mm", nullptr}, {"robot_y_mm", nullptr}, {"robot_z_mm", nullptr},
        {"mask_area_px", mask.pixels.size()}, {"segmentation", segmentation(mask)}, {"diameter_mm", nullptr}};
    if (value) {
        result["x_mm"] = rounded((mask.u-camera.cx)*(*value)/camera.fx, 1);
        result["y_mm"] = rounded((mask.v-camera.cy)*(*value)/camera.fy, 1);
        const auto robot = camera.robot(mask.u, mask.v, *value);
        result["robot_x_mm"] = rounded(robot[0], 1); result["robot_y_mm"] = rounded(robot[1], 1);
        result["robot_z_mm"] = rounded(robot[2], 1);
        result["diameter_mm"] = rounded(2*std::sqrt(mask.pixels.size()/std::acos(-1.0))*(*value)/camera.fx, 1);
    }
    if (depths.size() > 1) { result["z_frames_mm"] = frames; result["robot_z_frames_mm"] = robot_frames; }
    if (is_base) result["depth_quality"] = latest.quality;
    return result;
}

void mark_bases(json& objects, const std::vector<std::array<double, 4>>& boxes) {
    for (std::size_t i = 0; i < objects.size(); ++i) {
        auto& object = objects[i];
        if (object.at("class") != "CUP BASE") continue;
        const double u = object.at("x_px").get<double>(), v = object.at("y_px").get<double>();
        const auto& base = boxes[i]; const double mx = (base[2]-base[0])/2, my = (base[3]-base[1])/2;
        bool in_cup = false, near_handle = false;
        for (std::size_t j = 0; j < objects.size(); ++j) {
            const auto& box = boxes[j];
            if (objects[j].at("class") == "CUP")
                in_cup |= box[0] <= u && u <= box[2] && box[1] <= v && v <= box[3];
            if (objects[j].at("class") == "CUP HANDLE")
                near_handle |= box[0] <= base[2]+mx && box[2] >= base[0]-mx &&
                               box[1] <= base[3]+my && box[3] >= base[1]-my;
        }
        const auto& z = object.at("robot_z_mm");
        const bool cup_height = !z.is_null() && z.get<double>() >= kCupZMin && z.get<double>() <= kCupZMax;
        object["in_cup"] = in_cup; object["near_handle"] = near_handle;
        object["cup_height"] = cup_height; object["is_cup"] = in_cup || near_handle || cup_height;
    }
}
}  // namespace

nlohmann::json describe_objects(const nlohmann::json& detections,
                               const std::vector<unsigned char>& masks,
                               const std::vector<cv::Mat>& depths,
                               const nlohmann::json& config) {
    if (!detections.is_object()) invalid("Invalid detection document");
    const int width = integer(detections.at("width"), 1, kMaxWidth);
    const int height = integer(detections.at("height"), 1, kMaxHeight);
    if (depths.empty() || depths.size() > 9) invalid("Expected one to nine depth frames");
    for (const auto& depth : depths)
        if (depth.type() != CV_16UC1 || depth.cols != width || depth.rows != height)
            invalid("Depth samples must be aligned CV_16UC1 frames");
    const auto camera = geometry(config, width, height);
    const auto& names = config.at("names");
    if (!names.is_array() || names.size() < 3 || names.size() > 4) invalid("Invalid class names");
    std::set<std::string> unique_names;
    for (const auto& name : names) {
        if (!name.is_string() || name.get_ref<const std::string&>().empty()) invalid("Invalid class name");
        unique_names.insert(name.get<std::string>());
    }
    if (unique_names.size() != names.size() || !unique_names.count("CUP") ||
        !unique_names.count("CUP BASE") || !unique_names.count("CUP HANDLE")) invalid("Missing or duplicate cup classes");
    const auto& items = detections.at("detections");
    if (!items.is_array() || items.size() > kMaxDetections || masks.size() > kMaxMaskBytes)
        invalid("Detection payload exceeds limit");
    json objects = json::array();
    std::vector<std::array<double, 4>> boxes;
    std::size_t offset = 0, retained_crop_pixels = 0;
    for (std::size_t index = 0; index < items.size(); ++index) {
        const auto& item = items[index];
        const int class_id = integer(item.at("class_id"), 0, static_cast<int>(names.size())-1);
        const double score = number(item.at("score"));
        if (score < 0 || score > 1) invalid("Invalid detection confidence");
        const auto& raw_box = item.at("box_xyxy"); const auto& roi = item.at("mask_bbox");
        if (!raw_box.is_array() || raw_box.size() != 4 || !roi.is_array() || roi.size() != 4)
            invalid("Invalid detection geometry");
        std::array<double, 4> box{};
        for (int i = 0; i < 4; ++i) box[i] = number(raw_box[i]);
        if (!(box[0] >= 0 && box[1] >= 0 && box[0] < box[2] && box[1] < box[3] && box[2] <= width && box[3] <= height))
            invalid("Detection box exceeds frame");
        const int x = integer(roi[0], 0, width-1), y = integer(roi[1], 0, height-1);
        const int w = integer(roi[2], 1, width-x), h = integer(roi[3], 1, height-y);
        if (!(x >= std::ceil(box[0]) && y >= std::ceil(box[1]) && x+w <= std::ceil(box[2]) && y+h <= std::ceil(box[3])))
            invalid("Mask rectangle exceeds detection box");
        const auto size = static_cast<std::size_t>(w)*h;
        if (integer(item.at("mask_offset"), 0, static_cast<int>(kMaxMaskBytes)) != static_cast<int>(offset) ||
            integer(item.at("mask_length"), 1, static_cast<int>(kMaxMaskBytes)) != static_cast<int>(size) ||
            offset > masks.size() || size > masks.size()-offset) invalid("Invalid mask payload offsets");
        for (std::size_t i = offset; i < offset+size; ++i) if (masks[i] > 1) invalid("Mask values must be binary");
        const cv::Mat raw(h, w, CV_8U, const_cast<unsigned char*>(masks.data()+offset));
        offset += size;
        const std::string name = names[class_id].get<std::string>();
        if (name != "CUP" && name != "CUP BASE" && name != "CUP HANDLE") continue;
        // Bound crop-local distance maps, coordinate vectors and encoded masks
        // before mask_geometry allocates them, including sparse oversized ROIs.
        if (size > kMaxObjectCropPixels || size > kMaxRetainedCropPixels-retained_crop_pixels)
            invalid("Retained object masks exceed perception resource limits");
        retained_crop_pixels += size;
        // Detector.analyse skips empty masks without renumbering detections.
        if (!cv::countNonZero(raw)) continue;
        const auto mask = mask_geometry(raw, x, y, width, height, name == "CUP BASE");
        auto object = describe(name, score, mask, depths, camera);
        object["detection_id"] = index;
        object["bbox_xyxy"] = {rounded(box[0], 1), rounded(box[1], 1), rounded(box[2], 1), rounded(box[3], 1)};
        objects.push_back(std::move(object)); boxes.push_back(box);
    }
    if (offset != masks.size()) invalid("Unclaimed mask bytes");
    mark_bases(objects, boxes);
    return objects;
}

}  // namespace lynx
