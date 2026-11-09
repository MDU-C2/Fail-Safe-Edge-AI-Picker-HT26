#pragma once

#include <map>
#include <mutex>
#include <optional>
#include <string>

#include "util/time.hpp"

namespace gw {

// ================================================================
// Control lease
// ================================================================
//
// The single lease of gateway/control/manager.py. A strictly higher
// priority preempts the holder. A free lease (none yet, expired or
// released) goes to the highest-priority client that is contending:
// one that tried to acquire or renew within contention_seconds; a
// lower one is refused meanwhile. For startup_grace_seconds after
// creation nobody gets it and attempts are only recorded.
//
// Expiry and the windows run on the steady clock; the wall clock
// only gives the acquired_at / expires_at the API reports.
//

struct ControlLease {
    std::string client_id;
    long long priority = 0;

    WallTime acquired_at;
    WallTime expires_at;

    SteadyTime expires;
};

struct AcquireResult {
    bool success = false;
    std::string message;

    std::optional<ControlLease> lease;

    std::optional<std::string> previous_client_id;

    //
    // Seconds after which trying again can succeed, when the
    // refusal is only a matter of waiting.
    //
    std::optional<double> retry_after;
};

struct RenewResult {
    bool success = false;
    std::string message;

    std::optional<ControlLease> lease;
};

struct ReleaseResult {
    bool success = false;
    std::string message;
};

class ControlManager {
public:
    ControlManager(
        double lease_seconds,
        double contention_seconds = 0.0,
        double startup_grace_seconds = 0.0,
        const Clock& clock = system_clock()
    );

    std::optional<ControlLease> get() const;

    AcquireResult acquire(
        const std::string& client_id,
        long long priority
    );

    RenewResult renew(
        const std::string& client_id,
        long long priority
    );

    ReleaseResult release(
        const std::string& client_id
    );

    bool owns_control(
        const std::string& client_id
    ) const;

    double lease_seconds() const {
        return lease_seconds_;
    }

private:
    struct Attempt {
        long long priority = 0;
        SteadyTime at;
    };

    struct Contender {
        std::string client_id;
        double remaining_seconds = 0.0;
    };

    void remove_expired_locked(
        SteadyTime now
    ) const;

    std::optional<Contender> higher_contender_locked(
        long long priority,
        SteadyTime now
    ) const;

    ControlLease new_lease(
        const std::string& client_id,
        long long priority,
        SteadyTime now
    ) const;

    const Clock& clock_;

    double lease_seconds_;
    double contention_seconds_;

    SteadyTime grace_until_;

    mutable std::mutex mutex_;

    mutable std::optional<ControlLease> lease_;

    std::map<std::string, Attempt> attempts_;
};

}  // namespace gw
