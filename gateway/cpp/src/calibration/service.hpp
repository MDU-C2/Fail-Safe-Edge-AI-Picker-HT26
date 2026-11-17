#pragma once

#include <cstddef>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>

#include "http/message.hpp"
#include "util/time.hpp"

namespace gw {

// ================================================================
// Calibration
// ================================================================
//
// gateway/calibration/service.py: the calibration JSON object the
// controlling client posts, kept in memory and written atomically
// to a file (a temporary file renamed over the old one) in the same
// format as the Python gateway, {"calibration", "updated_at",
// "updated_by"} indented by 2, so either implementation reads the
// other's file. A file that is there but not valid throws
// CalibrationError at startup.
//

class CalibrationError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

struct StoredCalibration {
    http::Json calibration;

    WallTime updated_at;

    std::string updated_by;

    http::Json to_json() const;
};

class CalibrationService {
public:
    explicit CalibrationService(
        std::string filename,
        const Clock& clock = system_clock()
    );

    std::optional<StoredCalibration> get() const;

    //
    // Stores and writes it; throws CalibrationError if the file
    // cannot be written (the previous calibration stays).
    //
    StoredCalibration set(
        http::Json calibration,
        const std::string& client_id
    );

private:
    std::optional<StoredCalibration> load() const;

    std::string path_;

    const Clock& clock_;

    mutable std::mutex mutex_;

    std::optional<StoredCalibration> current_;
};

//
// Length of Python's json.dumps(value): ", " and ": " separators,
// non-ASCII as \uXXXX. The upload limit is measured this way.
//
std::size_t python_json_length(
    const http::Json& value
);

}  // namespace gw
