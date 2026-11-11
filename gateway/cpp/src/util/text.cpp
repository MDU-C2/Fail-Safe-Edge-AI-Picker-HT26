#include "util/text.hpp"

#include <cctype>
#include <cerrno>
#include <cstdarg>
#include <cstdio>
#include <cstdlib>
#include <vector>

namespace gw {

std::string_view trim(
    std::string_view text
) {
    while (!text.empty() && std::isspace(static_cast<unsigned char>(text.front()))) {
        text.remove_prefix(1);
    }

    while (!text.empty() && std::isspace(static_cast<unsigned char>(text.back()))) {
        text.remove_suffix(1);
    }

    return text;
}

std::string to_lower(
    std::string_view text
) {
    std::string result(text);

    for (char& c : result) {
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    }

    return result;
}

std::string to_upper(
    std::string_view text
) {
    std::string result(text);

    for (char& c : result) {
        c = static_cast<char>(std::toupper(static_cast<unsigned char>(c)));
    }

    return result;
}

bool iequals(
    std::string_view a,
    std::string_view b
) {
    if (a.size() != b.size()) {
        return false;
    }

    for (std::size_t i = 0; i < a.size(); ++i) {
        if (std::tolower(static_cast<unsigned char>(a[i])) != std::tolower(static_cast<unsigned char>(b[i]))) {
            return false;
        }
    }

    return true;
}

bool starts_with(
    std::string_view text,
    std::string_view prefix
) {
    return text.substr(0, prefix.size()) == prefix;
}

std::string format(
    const char* pattern,
    ...
) {
    va_list arguments;

    va_start(arguments, pattern);

    va_list copy;

    va_copy(copy, arguments);

    const int length = std::vsnprintf(nullptr, 0, pattern, copy);

    va_end(copy);

    std::string result;

    if (length > 0) {
        result.resize(static_cast<std::size_t>(length));

        std::vsnprintf(result.data(), result.size() + 1, pattern, arguments);
    }

    va_end(arguments);

    return result;
}

std::string format_g(
    double value
) {
    return format("%g", value);
}

std::optional<double> parse_double(
    std::string_view text
) {
    const std::string copy(trim(text));

    if (copy.empty()) {
        return std::nullopt;
    }

    char* end = nullptr;

    errno = 0;

    const double value = std::strtod(copy.c_str(), &end);

    if (errno == ERANGE || end != copy.c_str() + copy.size()) {
        return std::nullopt;
    }

    return value;
}

std::optional<long long> parse_integer(
    std::string_view text
) {
    const std::string copy(trim(text));

    if (copy.empty()) {
        return std::nullopt;
    }

    char* end = nullptr;

    errno = 0;

    const long long value = std::strtoll(copy.c_str(), &end, 10);

    if (errno == ERANGE || end != copy.c_str() + copy.size()) {
        return std::nullopt;
    }

    return value;
}

namespace {

int hex_value(
    char c
) {
    if (c >= '0' && c <= '9') {
        return c - '0';
    }

    if (c >= 'a' && c <= 'f') {
        return c - 'a' + 10;
    }

    if (c >= 'A' && c <= 'F') {
        return c - 'A' + 10;
    }

    return -1;
}

}  // namespace

std::string url_decode(
    std::string_view text,
    bool plus_is_space
) {
    std::string result;

    result.reserve(text.size());

    for (std::size_t i = 0; i < text.size(); ++i) {
        const char c = text[i];

        if (c == '%' && i + 2 < text.size()) {
            const int high = hex_value(text[i + 1]);
            const int low = hex_value(text[i + 2]);

            if (high >= 0 && low >= 0) {
                result.push_back(static_cast<char>(high * 16 + low));
                i += 2;
                continue;
            }
        }

        if (c == '+' && plus_is_space) {
            result.push_back(' ');
            continue;
        }

        result.push_back(c);
    }

    return result;
}

std::string url_encode(
    std::string_view text
) {
    static constexpr char hex[] = "0123456789ABCDEF";

    std::string result;

    for (const char c : text) {
        const auto byte = static_cast<unsigned char>(c);

        if (std::isalnum(byte) || c == '-' || c == '_' || c == '.' || c == '~') {
            result.push_back(c);
        } else {
            result.push_back('%');
            result.push_back(hex[byte >> 4]);
            result.push_back(hex[byte & 0xf]);
        }
    }

    return result;
}

}  // namespace gw
