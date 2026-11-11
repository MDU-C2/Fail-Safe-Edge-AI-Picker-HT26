#pragma once

#include <optional>
#include <stdexcept>
#include <string>
#include <string_view>

namespace gw::rws {

// ================================================================
// Errors
// ================================================================

//
// A failed Robot Web Services call. status is the controller's HTTP
// status, or nothing when it could not be reached at all.
//
class RwsError : public std::runtime_error {
public:
    RwsError(
        std::optional<int> status,
        std::string message
    );

    std::optional<int> status;
    std::string message;
};

//
// The reason in a controller error response, as rws/client.py's
// _error_message: _embedded.status.msg of the JSON body, else the
// <span class="msg"> of an XHTML body, else the start of the text
// (or the reason phrase). The controller's own source location
// ("C:\...\file.cpp[123] ERROR: ") is removed.
//
std::string error_message(
    std::string_view body,
    std::string_view reason
);

//
// Removes every "<drive>:\...\name.cpp[<line>]" (with an optional
// ":" and "ERROR:") from a message, and trims it.
//
std::string strip_source_location(
    std::string_view message
);

std::string html_unescape(
    std::string_view text
);

}  // namespace gw::rws
