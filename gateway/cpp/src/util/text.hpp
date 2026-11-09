#pragma once

#include <optional>
#include <string>
#include <string_view>

namespace gw {

// ================================================================
// Text helpers
// ================================================================

std::string_view trim(
    std::string_view text
);

std::string to_lower(
    std::string_view text
);

std::string to_upper(
    std::string_view text
);

bool iequals(
    std::string_view a,
    std::string_view b
);

bool starts_with(
    std::string_view text,
    std::string_view prefix
);

//
// printf-style formatting into a std::string.
//
[[gnu::format(printf, 1, 2)]]
std::string format(
    const char* pattern,
    ...
);

//
// Python's "{value:g}".
//
std::string format_g(
    double value
);

//
// Whole text as a number, or nothing (surrounding whitespace is
// allowed, anything else is not).
//
std::optional<double> parse_double(
    std::string_view text
);

std::optional<long long> parse_integer(
    std::string_view text
);

//
// Percent-decoding of a URL path segment or query part; '+' is a
// space only in queries.
//
std::string url_decode(
    std::string_view text,
    bool plus_is_space
);

std::string url_encode(
    std::string_view text
);

}  // namespace gw
