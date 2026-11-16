#pragma once

#include <chrono>
#include <optional>
#include <string>
#include <string_view>

namespace gw {

// ================================================================
// Clocks
// ================================================================

//
// Durations and deadlines (lease expiry, frame age) use the steady
// clock, so an NTP step on the device cannot end or extend them.
// The wall clock is only used for the timestamps the API reports.
//
using SteadyTime = std::chrono::steady_clock::time_point;
using WallTime = std::chrono::system_clock::time_point;

using Seconds = std::chrono::duration<double>;

class Clock {
public:
    virtual ~Clock() = default;

    virtual SteadyTime steady() const;
    virtual WallTime wall() const;
};

//
// The process-wide real clock.
//
const Clock& system_clock();

template <typename Duration>
double to_seconds(
    Duration duration
) {
    return std::chrono::duration_cast<Seconds>(duration).count();
}

template <typename TimePoint>
TimePoint add_seconds(
    TimePoint time,
    double seconds
) {
    return time + std::chrono::duration_cast<typename TimePoint::duration>(Seconds(seconds));
}

// ================================================================
// Formatting
// ================================================================

//
// UTC with microseconds, as Pydantic writes a datetime into JSON:
// "2026-11-09T12:00:00.123456Z". Always six digits, so the strings
// also sort in time order.
//
std::string format_json_time(
    WallTime time
);

//
// UTC as Python's datetime.isoformat() with a timezone:
// "2026-11-09T12:00:00.123456+00:00" (used in X-Frame-Timestamp).
//
std::string format_iso_time(
    WallTime time
);

//
// An ISO 8601 date and time as Pydantic accepts it in a stored
// file: "2026-11-09T12:00:00[.ffffff][Z|+HH:MM|-HH:MM]" ("T" or a
// space; no zone means UTC; more than six fraction digits are cut).
//
std::optional<WallTime> parse_iso_time(
    std::string_view text
);

}  // namespace gw
