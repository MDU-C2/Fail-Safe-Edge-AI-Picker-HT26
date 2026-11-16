#include "control/manager.hpp"

namespace gw {

ControlManager::ControlManager(
    double lease_seconds,
    double contention_seconds,
    double startup_grace_seconds,
    const Clock& clock
)
    : clock_(clock),
      lease_seconds_(lease_seconds),
      contention_seconds_(contention_seconds),
      grace_until_(add_seconds(clock.steady(), startup_grace_seconds)) {}

// ================================================================
// Internals
// ================================================================

void ControlManager::remove_expired_locked(
    SteadyTime now
) const {
    if (lease_ && lease_->expires <= now) {
        lease_.reset();
    }
}

std::optional<ControlManager::Contender> ControlManager::higher_contender_locked(
    long long priority,
    SteadyTime now
) const {
    const Seconds window(contention_seconds_);

    //
    // The highest priority above `priority` that tried within the
    // window; ties go to the larger client id, as Python's max()
    // over (priority, client_id, at).
    //
    const std::string* best_id = nullptr;
    const Attempt* best = nullptr;

    for (const auto& [client_id, attempt] : attempts_) {
        if (attempt.priority <= priority || now - attempt.at >= window) {
            continue;
        }

        if (
            best == nullptr
            || attempt.priority > best->priority
            || (attempt.priority == best->priority && client_id > *best_id)
        ) {
            best_id = &client_id;
            best = &attempt;
        }
    }

    if (best == nullptr) {
        return std::nullopt;
    }

    return Contender{
        *best_id,
        to_seconds(best->at + std::chrono::duration_cast<SteadyTime::duration>(window) - now),
    };
}

ControlLease ControlManager::new_lease(
    const std::string& client_id,
    long long priority,
    SteadyTime now
) const {
    const WallTime wall = clock_.wall();

    return ControlLease{
        client_id,
        priority,
        wall,
        add_seconds(wall, lease_seconds_),
        add_seconds(now, lease_seconds_),
    };
}

// ================================================================
// Lease
// ================================================================

std::optional<ControlLease> ControlManager::get() const {
    const std::lock_guard lock(mutex_);

    remove_expired_locked(clock_.steady());

    return lease_;
}

AcquireResult ControlManager::acquire(
    const std::string& client_id,
    long long priority
) {
    const std::lock_guard lock(mutex_);

    const SteadyTime now = clock_.steady();

    remove_expired_locked(now);

    attempts_[client_id] = Attempt{priority, now};

    if (now < grace_until_) {
        AcquireResult result;

        result.message = "Gateway is starting; control is handed out once the startup grace period ends";
        result.retry_after = to_seconds(grace_until_ - now);

        return result;
    }

    if (!lease_) {
        if (const auto contender = higher_contender_locked(priority, now)) {
            AcquireResult result;

            result.message = "Higher-priority client " + contender->client_id + " is contending for control";
            result.retry_after = contender->remaining_seconds;

            return result;
        }

        lease_ = new_lease(client_id, priority, now);

        AcquireResult result;

        result.success = true;
        result.message = "Control acquired";
        result.lease = lease_;

        return result;
    }

    if (lease_->client_id == client_id) {
        AcquireResult result;

        result.message = "Client already owns control; renew the lease instead";
        result.lease = lease_;

        return result;
    }

    if (priority > lease_->priority) {
        const std::string previous = lease_->client_id;

        lease_ = new_lease(client_id, priority, now);

        AcquireResult result;

        result.success = true;
        result.message = "Control acquired";
        result.lease = lease_;
        result.previous_client_id = previous;

        return result;
    }

    AcquireResult result;

    result.message = "Control is owned by an equal or higher-priority client";
    result.lease = lease_;

    return result;
}

RenewResult ControlManager::renew(
    const std::string& client_id,
    long long priority
) {
    const std::lock_guard lock(mutex_);

    const SteadyTime now = clock_.steady();

    remove_expired_locked(now);

    //
    // A client renewing a lease it lost is still alive and wants
    // control, so it counts as contending.
    //
    attempts_[client_id] = Attempt{priority, now};

    if (!lease_) {
        return RenewResult{false, "No active control lease", std::nullopt};
    }

    if (lease_->client_id != client_id) {
        return RenewResult{false, "Client does not own control", lease_};
    }

    lease_->expires_at = add_seconds(clock_.wall(), lease_seconds_);
    lease_->expires = add_seconds(now, lease_seconds_);

    return RenewResult{true, "Control lease renewed", lease_};
}

ReleaseResult ControlManager::release(
    const std::string& client_id
) {
    const std::lock_guard lock(mutex_);

    remove_expired_locked(clock_.steady());

    //
    // Releasing (even without holding the lease) withdraws the
    // client, so the next one need not wait for it.
    //
    attempts_.erase(client_id);

    if (!lease_) {
        return ReleaseResult{false, "No active control lease"};
    }

    if (lease_->client_id != client_id) {
        return ReleaseResult{false, "Client does not own control"};
    }

    lease_.reset();

    return ReleaseResult{true, "Control released"};
}

bool ControlManager::owns_control(
    const std::string& client_id
) const {
    const auto lease = get();

    return lease && lease->client_id == client_id;
}

}  // namespace gw
