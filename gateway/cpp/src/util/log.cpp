#include "util/log.hpp"

#include <cstdio>
#include <mutex>

namespace gw::log {

namespace {

std::mutex mutex;

void write(
    const char* level,
    std::string_view message
) {
    const std::lock_guard lock(mutex);

    std::fprintf(
        stdout,
        "%s: %.*s\n",
        level,
        static_cast<int>(message.size()),
        message.data()
    );

    std::fflush(stdout);
}

}  // namespace

void info(
    std::string_view message
) {
    write("INFO", message);
}

void warning(
    std::string_view message
) {
    write("WARNING", message);
}

void error(
    std::string_view message
) {
    write("ERROR", message);
}

}  // namespace gw::log
