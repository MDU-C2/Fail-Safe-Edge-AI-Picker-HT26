#pragma once

#include <string_view>

namespace gw::log {

// ================================================================
// Logging
// ================================================================
//
// One line per message on stdout, flushed at once, like the Python
// gateway's print(..., flush=True) and uvicorn's log: the test
// harness and docker logs read stdout.
//

void info(
    std::string_view message
);

void warning(
    std::string_view message
);

void error(
    std::string_view message
);

}  // namespace gw::log
