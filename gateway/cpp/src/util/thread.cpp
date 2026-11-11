#include "util/thread.hpp"

#include <pthread.h>

#include <memory>

namespace gw {

namespace {

void* run(
    void* argument
) {
    const std::unique_ptr<std::function<void()>> body(
        static_cast<std::function<void()>*>(argument)
    );

    (*body)();

    return nullptr;
}

}  // namespace

bool start_detached_thread(
    std::function<void()> body,
    std::size_t stack_bytes
) {
    pthread_attr_t attributes;

    if (pthread_attr_init(&attributes) != 0) {
        return false;
    }

    pthread_attr_setstacksize(&attributes, stack_bytes);
    pthread_attr_setdetachstate(&attributes, PTHREAD_CREATE_DETACHED);

    auto* argument = new std::function<void()>(std::move(body));

    pthread_t thread;

    const int result = pthread_create(&thread, &attributes, run, argument);

    pthread_attr_destroy(&attributes);

    if (result != 0) {
        delete argument;

        return false;
    }

    return true;
}

}  // namespace gw
