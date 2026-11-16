#pragma once

#include <cstddef>
#include <functional>

namespace gw {

// ================================================================
// Threads with a small stack
// ================================================================
//
// std::thread gives every thread the default 8 MB stack. Most of it
// is never touched, but on a device with little RAM and no swap the
// gateway's threads (one per connection) get a fixed, small one.
//

constexpr std::size_t DEFAULT_STACK_BYTES = 256 * 1024;

//
// Starts a detached thread; false if the system refused one.
//
bool start_detached_thread(
    std::function<void()> body,
    std::size_t stack_bytes = DEFAULT_STACK_BYTES
);

}  // namespace gw
