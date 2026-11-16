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

namespace {

//
// count digits at text[position] as a number, or -1.
//
int read_digits(
    std::string_view text,
    std::size_t position,
    std::size_t count
) {
    if (position + count > text.size()) {
        return -1;
    }

    int value = 0;

    for (std::size_t i = position; i < position + count; ++i) {
        if (text[i] < '0' || text[i] > '9') {
            return -1;
        }

        value = value * 10 + (text[i] - '0');
    }

    return value;
}

}  // namespace

std::optional<WallTime> parse_iso_time(
    std::string_view text
) {
    // YYYY-MM-DDTHH:MM:SS
    // 0123456789012345678

    if (text.size() < 19 || text[4] != '-' || text[7] != '-' || (text[10] != 'T' && text[10] != ' ')
        || text[13] != ':' || text[16] != ':') {
        return std::nullopt;
    }

    std::tm utc{};

    const int year = read_digits(text, 0, 4);
    const int month = read_digits(text, 5, 2);
    const int day = read_digits(text, 8, 2);
    const int hour = read_digits(text, 11, 2);
    const int minute = read_digits(text, 14, 2);
    const int second = read_digits(text, 17, 2);

    if (year < 0 || month < 1 || month > 12 || day < 1 || day > 31 || hour < 0 || hour > 23
        || minute < 0 || minute > 59 || second < 0 || second > 59) {
        return std::nullopt;
    }

    utc.tm_year = year - 1900;
    utc.tm_mon = month - 1;
    utc.tm_mday = day;
    utc.tm_hour = hour;
    utc.tm_min = minute;
    utc.tm_sec = second;

    std::size_t position = 19;

    long micros = 0;

    if (position < text.size() && text[position] == '.') {
        ++position;

        const std::size_t start = position;
        long scale = 100'000;

        while (position < text.size() && text[position] >= '0' && text[position] <= '9') {
            if (scale > 0) {
                micros += (text[position] - '0') * scale;
                scale /= 10;
            }

            ++position;
        }

        if (position == start) {
            return std::nullopt;
        }
    }

    long offset_seconds = 0;

    if (position < text.size()) {
        const char zone = text[position];

        if ((zone == 'Z' || zone == 'z') && position + 1 == text.size()) {
            position += 1;
        } else if ((zone == '+' || zone == '-') && position + 6 == text.size() && text[position + 3] == ':') {
            const int hours = read_digits(text, position + 1, 2);
            const int minutes = read_digits(text, position + 4, 2);

            if (hours < 0 || minutes < 0) {
                return std::nullopt;
            }

            offset_seconds = (zone == '+' ? 1 : -1) * (hours * 3600L + minutes * 60L);
            position += 6;
        } else {
            return std::nullopt;
        }
    }

    const std::time_t seconds = timegm(&utc);

    return WallTime(std::chrono::seconds(seconds - offset_seconds))
        + std::chrono::duration_cast<WallTime::duration>(std::chrono::microseconds(micros));
}

}  // namespace gw
