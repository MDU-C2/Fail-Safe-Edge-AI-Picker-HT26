#include "rws/errors.hpp"

#include <cctype>
#include <cstdlib>

#include <nlohmann/json.hpp>

#include "util/text.hpp"

namespace gw::rws {

RwsError::RwsError(
    std::optional<int> status_code,
    std::string text
)
    : std::runtime_error(text),
      status(status_code),
      message(std::move(text)) {}

namespace {

bool is_space(
    char c
) {
    return std::isspace(static_cast<unsigned char>(c)) != 0;
}

//
// Length of a source location starting at text[start], or 0. The
// pattern is that of client.py:
//     [A-Za-z]:\\\S*?\.cpp\[\d+\]:?\s*(ERROR:\s*)?
//
std::size_t source_location_at(
    std::string_view text,
    std::size_t start
) {
    if (
        start + 3 > text.size()
        || !std::isalpha(static_cast<unsigned char>(text[start]))
        || text[start + 1] != ':'
        || text[start + 2] != '\\'
    ) {
        return 0;
    }

    //
    // The shortest run of non-space characters that is followed by
    // ".cpp[<digits>]".
    //
    for (std::size_t i = start + 3; i < text.size() && !is_space(text[i]); ++i) {
        if (text.substr(i, 5) != ".cpp[") {
            continue;
        }

        std::size_t j = i + 5;

        const std::size_t digits = j;

        while (j < text.size() && std::isdigit(static_cast<unsigned char>(text[j]))) {
            ++j;
        }

        if (j == digits || j >= text.size() || text[j] != ']') {
            continue;
        }

        ++j;

        if (j < text.size() && text[j] == ':') {
            ++j;
        }

        while (j < text.size() && is_space(text[j])) {
            ++j;
        }

        if (text.substr(j, 6) == "ERROR:") {
            j += 6;

            while (j < text.size() && is_space(text[j])) {
                ++j;
            }
        }

        return j - start;
    }

    return 0;
}

}  // namespace

std::string strip_source_location(
    std::string_view message
) {
    std::string result;

    for (std::size_t i = 0; i < message.size();) {
        const std::size_t length = source_location_at(message, i);

        if (length > 0) {
            i += length;
        } else {
            result.push_back(message[i++]);
        }
    }

    return std::string(trim(result));
}

std::string html_unescape(
    std::string_view text
) {
    std::string result;

    for (std::size_t i = 0; i < text.size(); ++i) {
        if (text[i] != '&') {
            result.push_back(text[i]);
            continue;
        }

        const auto end = text.find(';', i);

        if (end == std::string_view::npos || end - i > 10) {
            result.push_back(text[i]);
            continue;
        }

        const std::string_view entity = text.substr(i + 1, end - i - 1);

        long code = -1;

        if (entity == "amp") {
            code = '&';
        } else if (entity == "lt") {
            code = '<';
        } else if (entity == "gt") {
            code = '>';
        } else if (entity == "quot") {
            code = '"';
        } else if (entity == "apos") {
            code = '\'';
        } else if (entity.size() > 1 && entity[0] == '#') {
            const bool hex = entity[1] == 'x' || entity[1] == 'X';
            const std::string digits(entity.substr(hex ? 2 : 1));

            char* parsed_end = nullptr;

            code = std::strtol(digits.c_str(), &parsed_end, hex ? 16 : 10);

            if (digits.empty() || *parsed_end != '\0') {
                code = -1;
            }
        }

        if (code < 0 || code > 0x10ffff) {
            result.push_back(text[i]);
            continue;
        }

        //
        // UTF-8
        //
        if (code < 0x80) {
            result.push_back(static_cast<char>(code));
        } else if (code < 0x800) {
            result.push_back(static_cast<char>(0xc0 | (code >> 6)));
            result.push_back(static_cast<char>(0x80 | (code & 0x3f)));
        } else if (code < 0x10000) {
            result.push_back(static_cast<char>(0xe0 | (code >> 12)));
            result.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3f)));
            result.push_back(static_cast<char>(0x80 | (code & 0x3f)));
        } else {
            result.push_back(static_cast<char>(0xf0 | (code >> 18)));
            result.push_back(static_cast<char>(0x80 | ((code >> 12) & 0x3f)));
            result.push_back(static_cast<char>(0x80 | ((code >> 6) & 0x3f)));
            result.push_back(static_cast<char>(0x80 | (code & 0x3f)));
        }

        i = end;
    }

    return result;
}

std::string error_message(
    std::string_view body,
    std::string_view reason
) {
    const auto json = nlohmann::json::parse(body, nullptr, false);

    std::string message;

    if (
        json.is_object()
        && json.contains("_embedded")
        && json["_embedded"].is_object()
        && json["_embedded"].contains("status")
        && json["_embedded"]["status"].is_object()
        && json["_embedded"]["status"].contains("msg")
        && json["_embedded"]["status"]["msg"].is_string()
    ) {
        message = json["_embedded"]["status"]["msg"].get<std::string>();
    } else {
        constexpr std::string_view open = "<span class=\"msg\">";

        const auto start = body.find(open);
        const auto end = start == std::string_view::npos
            ? std::string_view::npos
            : body.find("</span>", start + open.size());

        if (end == std::string_view::npos) {
            if (body.empty()) {
                return std::string(reason);
            }

            return std::string(body.substr(0, 300));
        }

        message = html_unescape(body.substr(start + open.size(), end - start - open.size()));
    }

    return strip_source_location(message);
}

}  // namespace gw::rws
