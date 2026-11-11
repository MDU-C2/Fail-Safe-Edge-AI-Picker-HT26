#include "calibration/service.hpp"

#include <cstdio>
#include <filesystem>
#include <fstream>
#include <sstream>

#include "util/text.hpp"

namespace gw {

using http::Json;

namespace fs = std::filesystem;

Json StoredCalibration::to_json() const {
    return Json{
        {"calibration", calibration},
        {"updated_at", format_json_time(updated_at)},
        {"updated_by", updated_by},
    };
}

std::size_t python_json_length(
    const Json& value
) {
    if (value.is_object()) {
        //
        // {"key": value, "key": value}
        //
        std::size_t length = 2;

        bool first = true;

        for (const auto& [key, item] : value.items()) {
            length += first ? 0 : 2;
            first = false;

            length += Json(key).dump(-1, ' ', true, nlohmann::detail::error_handler_t::replace).size();
            length += 2;
            length += python_json_length(item);
        }

        return length;
    }

    if (value.is_array()) {
        std::size_t length = 2;

        for (std::size_t i = 0; i < value.size(); ++i) {
            length += (i == 0 ? 0 : 2) + python_json_length(value[i]);
        }

        return length;
    }

    //
    // Scalars: nlohmann's ASCII-escaped form matches Python's for
    // strings, null, booleans and integers; floats are close.
    //
    return value.dump(-1, ' ', true, nlohmann::detail::error_handler_t::replace).size();
}

CalibrationService::CalibrationService(
    std::string filename,
    const Clock& clock
)
    : path_(std::move(filename)),
      clock_(clock),
      current_(load()) {}

std::optional<StoredCalibration> CalibrationService::load() const {
    std::error_code error;

    if (!fs::is_regular_file(path_, error)) {
        return std::nullopt;
    }

    std::ifstream file(path_, std::ios::binary);

    std::ostringstream content;

    content << file.rdbuf();

    auto fail = [&](const std::string& reason) {
        return CalibrationError(format("Calibration file is not valid: %s: %s", path_.c_str(), reason.c_str()));
    };

    const Json data = Json::parse(content.str(), nullptr, false);

    if (data.is_discarded()) {
        throw fail("not JSON");
    }

    if (!data.is_object()) {
        throw fail("not a JSON object");
    }

    if (!data.contains("calibration") || !data["calibration"].is_object()) {
        throw fail("'calibration' must be an object");
    }

    if (!data.contains("updated_by") || !data["updated_by"].is_string()) {
        throw fail("'updated_by' must be a string");
    }

    if (!data.contains("updated_at") || !data["updated_at"].is_string()) {
        throw fail("'updated_at' must be a date and time");
    }

    const auto updated_at = parse_iso_time(data["updated_at"].get<std::string>());

    if (!updated_at) {
        throw fail("'updated_at' must be a date and time");
    }

    return StoredCalibration{
        data["calibration"],
        *updated_at,
        data["updated_by"].get<std::string>(),
    };
}

std::optional<StoredCalibration> CalibrationService::get() const {
    const std::lock_guard lock(mutex_);

    return current_;
}

StoredCalibration CalibrationService::set(
    Json calibration,
    const std::string& client_id
) {
    StoredCalibration stored{
        std::move(calibration),
        clock_.wall(),
        client_id,
    };

    const std::lock_guard lock(mutex_);

    //
    // Write to a temporary file first and then replace the old one,
    // so a crash never leaves a half-written file.
    //
    const fs::path path(path_);

    std::error_code error;

    if (path.has_parent_path()) {
        fs::create_directories(path.parent_path(), error);
    }

    const fs::path temporary = path.string() + ".tmp";

    {
        std::ofstream file(temporary, std::ios::binary | std::ios::trunc);

        file << stored.to_json().dump(2, ' ', false, nlohmann::detail::error_handler_t::replace);

        file.flush();

        if (!file) {
            throw CalibrationError("Could not write the calibration file " + temporary.string());
        }
    }

    fs::rename(temporary, path, error);

    if (error) {
        throw CalibrationError("Could not replace the calibration file " + path_ + ": " + error.message());
    }

    current_ = stored;

    return stored;
}

}  // namespace gw
