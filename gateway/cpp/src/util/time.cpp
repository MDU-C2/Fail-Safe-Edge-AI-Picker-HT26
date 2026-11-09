#include "util/time.hpp"

#include <cstdio>
#include <ctime>

namespace gw {

SteadyTime Clock::steady() const {
    return std::chrono::steady_clock::now();
}

WallTime Clock::wall() const {
    return std::chrono::system_clock::now();
}

const Clock& system_clock() {
    static const Clock clock;

    return clock;
}

namespace {

std::string format_utc(
    WallTime time,
    bool microseconds,
    const char* suffix
) {
    const auto since_epoch = std::chrono::duration_cast<std::chrono::microseconds>(
        time.time_since_epoch()
    );

    auto seconds = since_epoch.count() / 1'000'000;
    auto micros = since_epoch.count() % 1'000'000;

    if (micros < 0) {
        micros += 1'000'000;
        seconds -= 1;
    }

    const std::time_t whole = static_cast<std::time_t>(seconds);

    std::tm utc{};

    gmtime_r(&whole, &utc);

    char buffer[48];

    if (microseconds) {
        std::snprintf(
            buffer,
            sizeof(buffer),
            "%04d-%02d-%02dT%02d:%02d:%02d.%06ld%s",
            utc.tm_year + 1900,
            utc.tm_mon + 1,
            utc.tm_mday,
            utc.tm_hour,
            utc.tm_min,
            utc.tm_sec,
            static_cast<long>(micros),
            suffix
        );
    } else {
        std::snprintf(
            buffer,
            sizeof(buffer),
            "%04d-%02d-%02dT%02d:%02d:%02d%s",
            utc.tm_year + 1900,
            utc.tm_mon + 1,
            utc.tm_mday,
            utc.tm_hour,
            utc.tm_min,
            utc.tm_sec,
            suffix
        );
    }

    return buffer;
}

}  // namespace

std::string format_json_time(
    WallTime time
) {
    return format_utc(time, true, "Z");
}

std::string format_iso_time(
    WallTime time
) {
    //
    // isoformat() leaves the fraction out when it is zero.
    //
    const auto micros = std::chrono::duration_cast<std::chrono::microseconds>(
        time.time_since_epoch()
    ).count() % 1'000'000;

    return format_utc(time, micros != 0, "+00:00");
}

}  // namespace gw
