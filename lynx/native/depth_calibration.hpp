#pragma once
#include "vendor/nlohmann/json.hpp"
#include <cmath>
#include <stdexcept>

namespace lynx {
// Hand-eye calibration fits optical Z before pinhole unprojection. It is
// independent of the camera-to-base transform's similarity scale.
struct DepthCalibration {
    double scale = 1., offset = 0.;

    static DepthCalibration from_json(const nlohmann::json& calibration) {
        DepthCalibration result;
        for (const auto* key : {"depth_scale", "depth_offset"}) {
            if (calibration.contains(key) && (!calibration[key].is_number() ||
                    !std::isfinite(calibration[key].get<double>())))
                throw std::invalid_argument("Depth calibration must contain finite numbers.");
        }
        result.scale = calibration.value("depth_scale", 1.);
        result.offset = calibration.value("depth_offset", 0.);
        if (result.scale <= 0.)
            throw std::invalid_argument("Depth calibration scale must be positive.");
        return result;
    }

    double correct(double raw) const {
        // Missing samples must not turn into a measured surface at offset mm.
        if (!std::isfinite(raw) || raw <= 0.) return 0.;
        const double value = scale * raw + offset;
        return std::isfinite(value) && value > 0. ? value : 0.;
    }
};
}
