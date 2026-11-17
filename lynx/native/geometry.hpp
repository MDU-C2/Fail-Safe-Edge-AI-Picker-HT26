#pragma once

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <stdexcept>
#include <vector>

namespace lynx {

constexpr int kMaskChannels = 32;

// Python round() uses ties-to-even. Image dimensions are small exact integers.
inline int python_round(double value) {
    const double low = std::floor(value);
    const double remainder = value - low;
    return static_cast<int>(remainder < 0.5 ? low : remainder > 0.5 ? low + 1 :
                            (static_cast<long long>(low) % 2 == 0 ? low : low + 1));
}

struct Geometry {
    int width, height, side, resized_width, resized_height, left, top;
    double gain;
};

inline Geometry letterbox_geometry(int width, int height, int side) {
    if (width <= 0 || height <= 0 || side < 32 || side > 640 || side % 32)
        throw std::runtime_error("Unsupported image or model dimensions");
    Geometry g{width, height, side, 0, 0, 0, 0,
               std::min(double(side) / width, double(side) / height)};
    g.resized_width = python_round(width * g.gain);
    g.resized_height = python_round(height * g.gain);
    if (!g.resized_width || !g.resized_height)
        throw std::runtime_error("Frame aspect ratio makes an empty model input");
    g.left = python_round((side - g.resized_width) / 2.0 - 0.1);
    g.top = python_round((side - g.resized_height) / 2.0 - 0.1);
    return g;
}

struct Detection {
    int label = 0;
    float score = 0;
    std::array<float, 4> box{};
    std::array<float, kMaskChannels> coefficients{};
};

inline float box_iou(const Detection& a, const Detection& b) {
    const auto& x = a.box;
    const auto& y = b.box;
    const float intersection = std::max(0.f, std::min(x[2], y[2]) - std::max(x[0], y[0])) *
                               std::max(0.f, std::min(x[3], y[3]) - std::max(x[1], y[1]));
    const float area_a = std::max(0.f, x[2] - x[0]) * std::max(0.f, x[3] - x[1]);
    const float area_b = std::max(0.f, y[2] - y[0]) * std::max(0.f, y[3] - y[1]);
    const float union_area = area_a + area_b - intersection;
    return union_area > 0 ? intersection / union_area : 0;
}

inline std::vector<Detection> class_aware_nms(std::vector<Detection> candidates,
                                            float threshold, std::size_t maximum) {
    std::stable_sort(candidates.begin(), candidates.end(),
                     [](const Detection& a, const Detection& b) { return a.score > b.score; });
    std::vector<Detection> selected;
    for (const auto& candidate : candidates) {
        bool overlaps = false;
        for (const auto& previous : selected) {
            if (candidate.label == previous.label && box_iou(candidate, previous) > threshold) {
                overlaps = true;
                break;
            }
        }
        if (!overlaps) {
            selected.push_back(candidate);
            if (selected.size() == maximum) break;
        }
    }
    return selected;
}

inline std::array<float, 4> original_box(const Detection& detection, const Geometry& g) {
    auto box = detection.box;
    for (int i = 0; i < 4; ++i)
        box[i] = std::clamp(float((box[i] - (i % 2 ? g.top : g.left)) / g.gain),
                            0.f, float(i % 2 ? g.height : g.width));
    return box;
}

struct MaskCrop {
    int x, y, width, height;
    std::vector<unsigned char> values;
};

// Same half-pixel sampling and edge replication as F.interpolate(...,
// mode="bilinear", align_corners=False). Never create a full-frame float mask.
inline float sample_bilinear(const std::vector<float>& data, int stride, int x, int y,
                             int width, int height, float sx, float sy) {
    sx = std::clamp(sx, 0.f, float(width - 1));
    sy = std::clamp(sy, 0.f, float(height - 1));
    const int x0 = static_cast<int>(std::floor(sx)), y0 = static_cast<int>(std::floor(sy));
    const int x1 = std::min(x0 + 1, width - 1), y1 = std::min(y0 + 1, height - 1);
    const float wx = sx - x0, wy = sy - y0;
    const float a = data[std::size_t(y + y0) * stride + x + x0];
    const float b = data[std::size_t(y + y0) * stride + x + x1];
    const float c = data[std::size_t(y + y1) * stride + x + x0];
    const float d = data[std::size_t(y + y1) * stride + x + x1];
    return (a * (1 - wx) + b * wx) * (1 - wy) + (c * (1 - wx) + d * wx) * wy;
}

inline MaskCrop original_mask(const std::vector<float>& logits, int proto_width, int proto_height,
                              const std::array<float, 4>& box, int width, int height) {
    if (proto_width <= 0 || proto_height <= 0 || width <= 0 || height <= 0 ||
        logits.size() != std::size_t(proto_width) * proto_height)
        throw std::runtime_error("Invalid mask geometry");
    for (float value : logits)
        if (!std::isfinite(value)) throw std::runtime_error("Nonfinite mask logits");
    for (float value : box)
        if (!std::isfinite(value)) throw std::runtime_error("Nonfinite mask box");
    // Ultralytics v8.4.166 process_mask_native -> scale_masks: crop prototype
    // padding, bilinear-resize logits, threshold >0, then crop original-space box.
    const double gain = std::min(double(proto_height) / height, double(proto_width) / width);
    const int content_width = python_round(width * gain);
    const int content_height = python_round(height * gain);
    const int left = python_round((proto_width - content_width) / 2.0 - 0.1);
    const int top = python_round((proto_height - content_height) / 2.0 - 0.1);
    if (content_width <= 0 || content_height <= 0)
        throw std::runtime_error("Frame aspect ratio makes an empty prototype crop");
    const int x0 = std::clamp(static_cast<int>(std::ceil(box[0])), 0, width);
    const int y0 = std::clamp(static_cast<int>(std::ceil(box[1])), 0, height);
    const int x1 = std::clamp(static_cast<int>(std::ceil(box[2])), x0, width);
    const int y1 = std::clamp(static_cast<int>(std::ceil(box[3])), y0, height);
    MaskCrop result{x0, y0, x1 - x0, y1 - y0, {}};
    result.values.resize(std::size_t(result.width) * result.height);
    const float scale_x = float(content_width) / width, scale_y = float(content_height) / height;
    for (int y = y0; y < y1; ++y) {
        const float sy = (y + 0.5f) * scale_y - 0.5f;
        for (int x = x0; x < x1; ++x) {
            const float sx = (x + 0.5f) * scale_x - 0.5f;
            const float value = sample_bilinear(logits, proto_width, left, top,
                                                content_width, content_height, sx, sy);
            result.values[std::size_t(y - y0) * result.width + x - x0] = value > 0 ? 1 : 0;
        }
    }
    return result;
}

}  // namespace lynx
