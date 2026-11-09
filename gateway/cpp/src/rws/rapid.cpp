#include "rws/rapid.hpp"

#include <cctype>

#include "util/text.hpp"

namespace gw::rws {

using http::HttpError;
using http::Json;

namespace {

bool is_space(
    char c
) {
    return std::isspace(static_cast<unsigned char>(c)) != 0;
}

bool is_alpha(
    char c
) {
    return (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
}

bool is_identifier(
    char c
) {
    return is_alpha(c) || (c >= '0' && c <= '9') || c == '_';
}

//
// Decodes one UTF-8 character at text[i]; advances i. Invalid bytes
// count as one character each (U+FFFD).
//
char32_t next_code_point(
    std::string_view text,
    std::size_t& i
) {
    const auto byte = static_cast<unsigned char>(text[i]);

    int length = 1;
    char32_t code = byte;

    if (byte >= 0xf0 && byte < 0xf8) {
        length = 4;
        code = byte & 0x07;
    } else if (byte >= 0xe0) {
        length = 3;
        code = byte & 0x0f;
    } else if (byte >= 0xc0) {
        length = 2;
        code = byte & 0x1f;
    } else if (byte >= 0x80) {
        ++i;
        return 0xfffd;
    }

    if (i + length > text.size()) {
        ++i;
        return 0xfffd;
    }

    for (int k = 1; k < length; ++k) {
        const auto continuation = static_cast<unsigned char>(text[i + k]);

        if ((continuation & 0xc0) != 0x80) {
            ++i;
            return 0xfffd;
        }

        code = (code << 6) | (continuation & 0x3f);
    }

    i += length;

    return code;
}

}  // namespace

std::size_t utf8_length(
    std::string_view utf8
) {
    std::size_t count = 0;

    for (std::size_t i = 0; i < utf8.size();) {
        next_code_point(utf8, i);
        ++count;
    }

    return count;
}

Latin1 to_latin1(
    std::string_view utf8
) {
    Latin1 result;

    result.bytes.reserve(utf8.size());

    for (std::size_t i = 0; i < utf8.size();) {
        const std::size_t start = i;
        const char32_t code = next_code_point(utf8, i);

        if (code <= 0xff) {
            result.bytes.push_back(static_cast<char>(code));
            continue;
        }

        if (!result.unencodable) {
            //
            // The run of consecutive characters it cannot hold, as
            // Python's UnicodeEncodeError reports it.
            //
            std::size_t end = i;

            while (end < utf8.size()) {
                std::size_t peek = end;

                if (next_code_point(utf8, peek) <= 0xff) {
                    break;
                }

                end = peek;
            }

            result.unencodable = std::string(utf8.substr(start, end - start));
        }
    }

    return result;
}

std::optional<std::string> declared_module(
    std::string_view source
) {
    for (std::size_t line = 0; line <= source.size();) {
        //
        // \s* also crosses blank lines, like the regular expression.
        //
        std::size_t i = line;

        while (i < source.size() && is_space(source[i])) {
            ++i;
        }

        if (iequals(source.substr(i, 6), "MODULE") && i + 6 < source.size() && is_space(source[i + 6])) {
            std::size_t j = i + 6;

            while (j < source.size() && is_space(source[j])) {
                ++j;
            }

            if (j < source.size() && is_alpha(source[j])) {
                std::size_t end = j;

                while (end < source.size() && is_identifier(source[end])) {
                    ++end;
                }

                return std::string(source.substr(j, end - j));
            }
        }

        const auto newline = source.find('\n', line);

        if (newline == std::string_view::npos) {
            break;
        }

        line = newline + 1;
    }

    return std::nullopt;
}

void check_source(
    const std::string& module,
    const std::string& source
) {
    const auto declared = declared_module(source);

    //
    // The controller names the module after the MODULE line, not
    // the file, so a mismatch would load it under another name.
    // RAPID names are case-insensitive.
    //
    if (!declared || !iequals(*declared, module)) {
        throw HttpError(422, "Source must declare MODULE " + module);
    }

    const Latin1 latin1 = to_latin1(source);

    if (latin1.unencodable) {
        throw HttpError(
            422,
            "Source contains characters the controller cannot store (ISO-8859-1): '" + *latin1.unencodable + "'"
        );
    }
}

void require_stopped_task(
    Controller& controller,
    const std::string& task
) {
    std::string names;
    bool known = false;

    for (const auto& entry : controller.tasks()) {
        const std::string name = entry.value("name", "");

        known = known || name == task;

        names += names.empty() ? "" : ", ";
        names += name;
    }

    if (!known) {
        throw HttpError(404, "Unknown task " + task + ", the controller has " + names);
    }

    if (controller.execution_state() != "stopped") {
        throw HttpError(409, "RAPID is running, stop it before changing modules");
    }
}

Json read_status(
    Controller& controller
) {
    Json tasks = Json::array();

    //
    // In the Python gateway's order: execution state first, then
    // the controller and mode, then the tasks.
    //
    const std::string execution_state = controller.execution_state();
    const std::string controller_state = controller.controller_state();
    const std::string operation_mode = controller.operation_mode();

    for (const auto& task : controller.tasks()) {
        const std::string name = task.value("name", "");

        tasks.push_back(
            Json{
                {"name", name},
                {"state", task.value("taskstate", "")},
                {"execution_state", task.value("excstate", "")},
                //
                // Background (semi-static) tasks such as YuMiLibBG_L
                // have no task selection switch and always run, so
                // the controller omits active for them.
                //
                {"active", task.value("active", "On") == "On"},
                {"modules", controller.program_modules(name)},
            }
        );
    }

    return Json{
        {"execution_state", execution_state},
        {"controller_state", controller_state},
        {"operation_mode", operation_mode},
        {"tasks", tasks},
    };
}

}  // namespace gw::rws
