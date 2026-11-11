#pragma once

#include <optional>
#include <string>
#include <string_view>

#include "http/message.hpp"
#include "rws/controller.hpp"

namespace gw::rws {

// ================================================================
// RAPID program (rws/rapid_router.py helpers)
// ================================================================

//
// {execution_state, controller_state, operation_mode, tasks: [{name,
// state, execution_state, active, modules}]}. Background tasks
// without an "active" field always run, so they count as active.
//
http::Json read_status(
    Controller& controller
);

//
// 422 (http::HttpError) unless the source declares MODULE <module>
// (case-insensitive, like RAPID) and can be stored as ISO-8859-1.
//
void check_source(
    const std::string& module,
    const std::string& source
);

//
// 404 for a task the controller does not have, 409 while RAPID
// runs (the controller would only answer with an error code).
//
void require_stopped_task(
    Controller& controller,
    const std::string& task
);

//
// The name in the first "MODULE <name>" at the start of a line, as
// the regular expression ^\s*MODULE\s+([A-Za-z][A-Za-z0-9_]*) with
// IGNORECASE | MULTILINE.
//
std::optional<std::string> declared_module(
    std::string_view source
);

//
// UTF-8 to ISO-8859-1 bytes; the first run of characters it cannot
// hold if there is one.
//
struct Latin1 {
    std::string bytes;
    std::optional<std::string> unencodable;
};

Latin1 to_latin1(
    std::string_view utf8
);

//
// Number of Unicode characters (Python's len) of UTF-8 text.
//
std::size_t utf8_length(
    std::string_view utf8
);

}  // namespace gw::rws
