#pragma once

#include <map>
#include <string>
#include <string_view>

namespace gw {

// ================================================================
// .env files
// ================================================================
//
// The subset of python-dotenv that gateway/.env uses: KEY=VALUE
// lines, an optional "export " prefix, # comments (whole lines, or
// after an unquoted value following whitespace), and single- or
// double-quoted values. Keys are returned as written.
//

std::map<std::string, std::string> parse_dotenv(
    std::string_view text
);

//
// The parsed file, or nothing if it cannot be read (a missing .env
// is fine: every setting has a default).
//
std::map<std::string, std::string> read_dotenv(
    const std::string& path
);

}  // namespace gw
