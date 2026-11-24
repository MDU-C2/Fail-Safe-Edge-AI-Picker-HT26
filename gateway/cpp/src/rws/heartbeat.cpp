#include "rws/heartbeat.hpp"

#include <chrono>
#include <random>

#include "rws/errors.hpp"
#include "util/log.hpp"
#include "util/text.hpp"

namespace gw::rws {

namespace {

//
// The variables are PERS of SharedVars, shared by every task that
// declares them, so one task is enough.
//
constexpr const char* TASK = "T_ROB_L";

//
// RAPID num is a float: integers stay exact up to 2^23.
//
constexpr std::uint32_t COUNTER_WRAP = 1000000;
constexpr std::uint32_t SESSION_MAX = 8000000;

std::uint32_t new_session() {
    std::random_device device;

    return std::uniform_int_distribution<std::uint32_t>(1, SESSION_MAX)(device);
}

}  // namespace

RwsHeartbeat::RwsHeartbeat(
    Controller& controller,
    double timeout_seconds,
    AutoHomeCallback on_auto_home,
    double interval_seconds
)
    : controller_(controller),
      timeout_seconds_(timeout_seconds),
      interval_seconds_(interval_seconds),
      on_auto_home_(std::move(on_auto_home)),
      session_(new_session()) {
    state_.enabled = timeout_seconds_ > 0;
}

RwsHeartbeat::~RwsHeartbeat() {
    stop();
}

void RwsHeartbeat::start() {
    if (timeout_seconds_ <= 0) {
        return;
    }

    {
        const std::lock_guard lock(mutex_);

        if (running_) {
            return;
        }

        running_ = true;
    }

    thread_ = std::thread([this]() { run(); });
}

void RwsHeartbeat::stop() {
    {
        const std::lock_guard lock(mutex_);

        if (!running_) {
            return;
        }

        running_ = false;
    }

    wake_.notify_all();

    if (thread_.joinable()) {
        thread_.join();
    }
}

void RwsHeartbeat::run() {
    std::unique_lock lock(mutex_);

    while (running_) {
        lock.unlock();

        beat();

        lock.lock();

        wake_.wait_for(lock, std::chrono::duration<double>(interval_seconds_), [this]() { return !running_; });
    }
}

// ================================================================
// One round
// ================================================================

void RwsHeartbeat::beat() {
    std::array<bool, 2> auto_home{};

    try {
        //
        // Every round, so it is back after rapidctl.py reset.
        //
        controller_.write_symbol(TASK, "gw_timeout", format_g(timeout_seconds_));

        if (!session_written_) {
            controller_.write_symbol(TASK, "gw_session", std::to_string(session_));
            session_written_ = true;
        }

        counter_ = counter_ % COUNTER_WRAP + 1;

        controller_.write_symbol(TASK, "gw_heartbeat", std::to_string(counter_));

        for (const Arm arm : {Arm::left, Arm::right}) {
            auto_home[arm == Arm::left ? 0 : 1] =
                controller_.read_symbol(arm_task(arm), std::string("auto_home_") + arm_name(arm)) == "TRUE";
        }

    } catch (const RwsError& error) {
        set_error(
            error.status == 400
                ? "The RAPID program has no gateway watchdog variables (gw_heartbeat); upload rapid/ again"
                : error.message
        );

        return;
    }

    std::array<bool, 2> started{};

    {
        const std::lock_guard lock(mutex_);

        if (!state_.connected) {
            log::info(format("RWS heartbeat: writing to the controller (session %u)", session_));
        }

        state_.connected = true;
        state_.error.reset();

        for (std::size_t i = 0; i < 2; ++i) {
            started[i] = auto_home[i] && state_.auto_home[i] != true;
            state_.auto_home[i] = auto_home[i];
        }
    }

    for (const Arm arm : {Arm::left, Arm::right}) {
        if (started[arm == Arm::left ? 0 : 1] && on_auto_home_) {
            on_auto_home_(arm);
        }
    }
}

void RwsHeartbeat::set_error(
    const std::string& message
) {
    const std::lock_guard lock(mutex_);

    //
    // Logged when it changes, not every second.
    //
    if (state_.connected || state_.error != message) {
        log::warning(format("RWS heartbeat failed: %s", message.c_str()));
    }

    state_.connected = false;
    state_.error = message;
}

RwsHeartbeat::State RwsHeartbeat::state() const {
    const std::lock_guard lock(mutex_);

    return state_;
}

}  // namespace gw::rws
