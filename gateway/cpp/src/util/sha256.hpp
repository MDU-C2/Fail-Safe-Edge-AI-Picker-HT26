#pragma once

#include <string>
#include <string_view>

namespace gw {

// ================================================================
// SHA-256
// ================================================================

//
// Lower-case hex digest, as Python's hashlib.sha256(...).hexdigest().
//
std::string sha256_hex(
    std::string_view data
);

//
// Compares in time independent of where the strings differ, like
// Python's secrets.compare_digest (the length may leak).
//
bool constant_time_equal(
    std::string_view a,
    std::string_view b
);

}  // namespace gw
