#include "config/dotenv.hpp"

#include <fstream>
#include <sstream>

#include "util/text.hpp"

namespace gw {

namespace {

std::string unquote(
    std::string_view value
) {
    value = trim(value);

    if (value.size() >= 2 && (value.front() == '"' || value.front() == '\'')) {
        const char quote = value.front();
        const auto end = value.find(quote, 1);

        if (end != std::string_view::npos) {
            std::string inner(value.substr(1, end - 1));

            if (quote == '"') {
                //
                // Double quotes understand the usual escapes.
                //
                std::string unescaped;

                for (std::size_t i = 0; i < inner.size(); ++i) {
                    if (inner[i] == '\\' && i + 1 < inner.size()) {
                        const char next = inner[++i];

                        unescaped.push_back(next == 'n' ? '\n' : next == 't' ? '\t' : next);
                    } else {
                        unescaped.push_back(inner[i]);
                    }
                }

                return unescaped;
            }

            return inner;
        }
    }

    //
    // Unquoted: a # after whitespace starts a comment.
    //
    for (std::size_t i = 1; i < value.size(); ++i) {
        if (value[i] == '#' && (value[i - 1] == ' ' || value[i - 1] == '\t')) {
            value = value.substr(0, i);
            break;
        }
    }

    return std::string(trim(value));
}

}  // namespace

std::map<std::string, std::string> parse_dotenv(
    std::string_view text
) {
    std::map<std::string, std::string> values;

    std::size_t start = 0;

    while (start <= text.size()) {
        auto end = text.find('\n', start);

        if (end == std::string_view::npos) {
            end = text.size();
        }

        std::string_view line = trim(text.substr(start, end - start));

        start = end + 1;

        if (line.empty() || line.front() == '#') {
            continue;
        }

        if (starts_with(line, "export ")) {
            line = trim(line.substr(7));
        }

        const auto equals = line.find('=');

        if (equals == std::string_view::npos) {
            continue;
        }

        const std::string_view key = trim(line.substr(0, equals));

        if (key.empty()) {
            continue;
        }

        values[std::string(key)] = unquote(line.substr(equals + 1));
    }

    return values;
}

std::map<std::string, std::string> read_dotenv(
    const std::string& path
) {
    std::ifstream file(path, std::ios::binary);

    if (!file) {
        return {};
    }

    std::ostringstream content;

    content << file.rdbuf();

    return parse_dotenv(content.str());
}

}  // namespace gw
