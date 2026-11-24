#pragma once

#include <array>
#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <mutex>
#include <optional>
#include <string>
#include <thread>

#include "rws/controller.hpp"
#include "rws/motion.hpp"

namespace gw::rws {

// ================================================================
// RWS heartbeat
// ================================================================
//
// Keeps the gateway watchdog of the RAPID arm tasks fed (see
// rapid/SharedVars.mod). Once a second it writes gw_timeout and
// gw_heartbeat + 1; the first successful round also writes a new
// gw_session. An idle arm goes home on its own when the heartbeat
// stops for gw_timeout seconds or the session changes (the gateway
// restarted), unless both arms hold something.
//
// Each round also reads auto_home_<arm> (TRUE: the arm went home on
// its own and has had no job since) for /health, and calls
// on_auto_home when it is TRUE and was not at the last read (also
// when the arm went home while the controller was out of reach).
//
// timeout_seconds 0 turns it off: nothing is written. A program
// without the variables is logged once and tried again every round.
//

class RwsHeartbeat {
public:
    using AutoHomeCallback = std::function<void(Arm)>;

    struct State {
        bool enabled = false;

        //
        // The last round wrote the heartbeat.
        //
        bool connected = false;

        std::optional<std::string> error;

        //
        // As last read; unknown until then.
        //
        std::array<std::optional<bool>, 2> auto_home{};
    };

    RwsHeartbeat(
        Controller& controller,
        double timeout_seconds,
        AutoHomeCallback on_auto_home = {},
        double interval_seconds = 1.0
    );

    ~RwsHeartbeat();

    void start();

    void stop();

    //
    // One round; the thread calls it every interval_seconds.
    //
    void beat();

    //
    // Writes gw_timeout and this gateway's gw_session now (nothing
    // when off). After the variables were reset to their declared
    // values, and before RAPID starts: the arm tasks take the
    // session they see at the start as this gateway's, so a session
    // written only later would look like a gateway restart and send
    // the arms home. Throws RwsError.
    //
    void restore();

    State state() const;

    std::uint32_t session() const {
        return session_;
    }

private:
    void run();

    void set_error(
        const std::string& message
    );

    Controller& controller_;

    double timeout_seconds_;
    double interval_seconds_;

    AutoHomeCallback on_auto_home_;

    std::uint32_t session_;
    std::uint32_t counter_ = 0;
    std::atomic<bool> session_written_ = false;

    mutable std::mutex mutex_;
    std::condition_variable wake_;
    bool running_ = false;
    std::thread thread_;

    State state_;
};

}  // namespace gw::rws
