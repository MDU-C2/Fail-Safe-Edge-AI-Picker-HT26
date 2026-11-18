#include "rws/queue.hpp"

#include <chrono>
#include <future>
#include <memory>
#include <set>
#include <stdexcept>

#include "rws/errors.hpp"
#include "util/text.hpp"
#include "util/thread.hpp"

namespace gw::rws {

namespace {

//
// "Job 3 (grip_cup)", for messages.
//
std::string job_label(
    std::size_t index,
    const std::string& name
) {
    return format("Job %zu (%s)", index + 1, name.c_str());
}

void check_job(
    const RwsMotion& motion,
    std::size_t index,
    const QueueJob& job
) {
    const std::string label = job_label(index, job.name);

    switch (job.type) {
    case QueueJobType::move:
        if (!motion.in_safe_zone(job.position)) {
            throw std::invalid_argument(label + ": target outside the safe zone");
        }

        break;

    case QueueJobType::path:
        if (job.points.empty() || job.points.size() > RwsMotion::MAX_PATH_POINTS) {
            throw std::invalid_argument(
                format("%s: a path has 1 to %zu points", label.c_str(), RwsMotion::MAX_PATH_POINTS)
            );
        }

        for (const PathPoint& point : job.points) {
            if (!motion.in_safe_zone(point.position)) {
                throw std::invalid_argument(label + ": a point outside the safe zone");
            }
        }

        break;

    case QueueJobType::home:
        break;

    case QueueJobType::gripper:
        if ((job.action == GripperAction::move) != job.grip_position.has_value()) {
            throw std::invalid_argument(label + ": position is required for move and only for move");
        }

        if (job.force && job.action != GripperAction::grip) {
            throw std::invalid_argument(label + ": force is only for grip");
        }

        break;
    }
}

}  // namespace

const char* job_type_name(
    QueueJobType type
) {
    static const char* const NAMES[] = {"move", "path", "home", "gripper"};

    return NAMES[static_cast<int>(type)];
}

const char* queue_state_name(
    QueueState state
) {
    static const char* const NAMES[] = {"idle", "running", "done", "failed", "cancelled"};

    return NAMES[static_cast<int>(state)];
}

const char* job_state_name(
    JobState state
) {
    static const char* const NAMES[] = {"pending", "running", "done", "failed", "skipped"};

    return NAMES[static_cast<int>(state)];
}

MotionQueue::MotionQueue(
    RwsMotion& motion,
    const Clock& clock
)
    : motion_(motion),
      clock_(clock) {}

MotionQueue::~MotionQueue() {
    std::unique_lock lock(mutex_);

    state_.cancel_requested = true;

    changed_.wait(lock, [this]() { return workers_ == 0; });
}

void MotionQueue::set_listener(
    Listener listener
) {
    const std::lock_guard lock(mutex_);

    listener_ = std::move(listener);
}

// ================================================================
// Start, cancel, clear
// ================================================================

QueueSnapshot MotionQueue::start(
    const std::string& owner,
    std::vector<QueueJob> jobs
) {
    if (jobs.empty() || jobs.size() > MAX_JOBS) {
        throw std::invalid_argument(format("A queue has 1 to %zu jobs", MAX_JOBS));
    }

    std::set<Arm> arms;

    for (std::size_t i = 0; i < jobs.size(); ++i) {
        check_job(motion_, i, jobs[i]);

        arms.insert(jobs[i].arm);
    }

    auto check_free = [this]() {
        if (state_.state == QueueState::running) {
            throw QueueConflict(
                format(
                    "A queue is running (owner %s, job %zu of %zu)",
                    state_.owner.value_or("").c_str(),
                    state_.step.value_or(0) + 1,
                    state_.jobs.size()
                )
            );
        }

        if (state_.state == QueueState::failed || state_.state == QueueState::cancelled) {
            throw QueueConflict(
                format(
                    "The last queue %s at job %zu of %zu; clear it first (DELETE /robot/rws/queue)",
                    state_.state == QueueState::failed ? "failed" : "was cancelled",
                    state_.step.value_or(0) + 1,
                    state_.jobs.size()
                )
            );
        }

        if (singles_ > 0) {
            throw QueueConflict("A single RWS job is in progress");
        }
    };

    {
        const std::lock_guard lock(mutex_);

        check_free();
    }

    //
    // Outside the lock: controller calls. RAPID must run and the
    // arms be idle, so the first job is not refused (which would
    // leave a failed queue that has moved nothing).
    //
    for (const Arm arm : arms) {
        motion_.check_idle(arm);
    }

    {
        std::unique_lock lock(mutex_);

        //
        // A worker that has just finished still publishes its last
        // event; this queue's come after it.
        //
        changed_.wait(lock, [this]() { return workers_ == 0; });

        check_free();

        QueueSnapshot state;

        state.state = QueueState::running;
        state.owner = owner;
        state.step = 0;
        state.started_at = clock_.wall();

        for (const QueueJob& job : jobs) {
            QueueJobStatus status;

            status.name = job.name;
            status.group = job.group;
            status.type = job.type;
            status.arm = job.arm;

            state.jobs.push_back(std::move(status));
        }

        state_ = std::move(state);
        jobs_ = std::move(jobs);
        failed_ = false;

        ++workers_;
    }

    notify("started");

    if (!start_detached_thread([this]() { work(); })) {
        {
            const std::lock_guard lock(mutex_);

            for (QueueJobStatus& job : state_.jobs) {
                job.state = JobState::skipped;
            }

            state_.state = QueueState::failed;
            state_.message = "The gateway could not start the queue's thread";
            state_.finished_at = clock_.wall();

            --workers_;
        }

        changed_.notify_all();

        notify("failed");
    }

    return snapshot();
}

QueueSnapshot MotionQueue::cancel_or_clear() {
    std::string event;

    {
        std::unique_lock lock(mutex_);

        if (state_.state != QueueState::running) {
            changed_.wait(lock, [this]() { return workers_ == 0; });
        }

        switch (state_.state) {
        case QueueState::idle:
            break;

        case QueueState::running:
            if (!state_.cancel_requested) {
                state_.cancel_requested = true;

                event = "cancelling";
            }

            break;

        case QueueState::done:
        case QueueState::failed:
        case QueueState::cancelled:
            state_ = QueueSnapshot{};
            jobs_.clear();

            event = "cleared";

            break;
        }
    }

    if (!event.empty()) {
        notify(event);
    }

    return snapshot();
}

// ================================================================
// Single jobs
// ================================================================

MotionQueue::SingleJob::SingleJob(
    MotionQueue& queue
)
    : queue_(queue) {
    const std::lock_guard lock(queue_.mutex_);

    if (queue_.state_.state == QueueState::running) {
        throw QueueConflict(
            format(
                "A queue is running (owner %s); single jobs wait until it is done or cancelled",
                queue_.state_.owner.value_or("").c_str()
            )
        );
    }

    ++queue_.singles_;
}

MotionQueue::SingleJob::~SingleJob() {
    const std::lock_guard lock(queue_.mutex_);

    --queue_.singles_;
}

MotionQueue::SingleJob MotionQueue::admit_single() {
    return SingleJob(*this);
}

// ================================================================
// State
// ================================================================

QueueSnapshot MotionQueue::snapshot() const {
    const std::lock_guard lock(mutex_);

    return snapshot_locked();
}

QueueSnapshot MotionQueue::snapshot_locked() const {
    return state_;
}

bool MotionQueue::running() const {
    const std::lock_guard lock(mutex_);

    return state_.state == QueueState::running;
}

bool MotionQueue::wait_until_finished(
    double timeout_seconds
) const {
    std::unique_lock lock(mutex_);

    return changed_.wait_for(
        lock,
        std::chrono::duration<double>(timeout_seconds),
        [this]() { return state_.state != QueueState::running && workers_ == 0; }
    );
}

void MotionQueue::notify(
    const std::string& event
) {
    Listener listener;
    QueueSnapshot snapshot;

    {
        const std::lock_guard lock(mutex_);

        listener = listener_;
        snapshot = snapshot_locked();
    }

    if (listener) {
        listener(event, snapshot);
    }
}

// ================================================================
// Worker
// ================================================================

void MotionQueue::work() {
    std::size_t count = 0;

    {
        const std::lock_guard lock(mutex_);

        count = jobs_.size();
    }

    //
    // jobs_ does not change while the worker runs (start and clear
    // refuse or wait meanwhile), so it is read without the lock.
    //
    std::size_t begin = 0;

    while (begin < count) {
        std::size_t end = begin + 1;

        if (jobs_[begin].group) {
            while (end < count && jobs_[end].group == jobs_[begin].group) {
                ++end;
            }
        }

        std::vector<std::size_t> lanes[2];

        for (std::size_t i = begin; i < end; ++i) {
            lanes[jobs_[i].arm == Arm::left ? 0 : 1].push_back(i);
        }

        bool ok = true;

        if (!lanes[0].empty() && !lanes[1].empty()) {
            //
            // The right lane on a thread of its own; this one runs
            // the left lane, then waits for it.
            //
            auto right = std::make_shared<std::promise<bool>>();
            auto right_ok = right->get_future();

            const bool started = start_detached_thread([this, right, &lanes]() {
                right->set_value(run_lane(lanes[1]));
            });

            const bool left_ok = run_lane(lanes[0]);

            if (started) {
                ok = right_ok.get() && left_ok;

            } else {
                ok = left_ok && run_lane(lanes[1]);
            }

        } else {
            ok = run_lane(lanes[0].empty() ? lanes[1] : lanes[0]);
        }

        if (!ok) {
            break;
        }

        begin = end;
    }

    std::string event;

    {
        const std::lock_guard lock(mutex_);

        bool skipped = false;

        for (QueueJobStatus& job : state_.jobs) {
            if (job.state == JobState::pending) {
                job.state = JobState::skipped;
                skipped = true;
            }
        }

        if (failed_) {
            state_.state = QueueState::failed;

        } else if (skipped) {
            state_.state = QueueState::cancelled;
            state_.message = "Cancelled";

            //
            // Where it stopped: the first job not run.
            //
            for (std::size_t i = 0; i < state_.jobs.size(); ++i) {
                if (state_.jobs[i].state == JobState::skipped) {
                    state_.step = i;
                    break;
                }
            }

        } else {
            state_.state = QueueState::done;
            state_.step.reset();
        }

        state_.finished_at = clock_.wall();

        event = queue_state_name(state_.state);
    }

    changed_.notify_all();

    notify(event);

    //
    // Last: the destructor waits for this.
    //
    {
        const std::lock_guard lock(mutex_);

        --workers_;
    }

    changed_.notify_all();
}

bool MotionQueue::run_lane(
    const std::vector<std::size_t>& lane
) {
    for (const std::size_t index : lane) {
        {
            const std::lock_guard lock(mutex_);

            if (failed_ || state_.cancel_requested) {
                return false;
            }

            state_.jobs[index].state = JobState::running;

            //
            // The first running job (the other lane may be ahead).
            //
            for (std::size_t i = 0; i < state_.jobs.size(); ++i) {
                if (state_.jobs[i].state == JobState::running) {
                    state_.step = i;
                    break;
                }
            }
        }

        CommandResult result;

        try {
            result = run_job(jobs_[index]);

        } catch (const RwsError& error) {
            result = CommandResult{false, "Robot controller: " + error.message};

        } catch (const std::exception& error) {
            result = CommandResult{false, error.what()};
        }

        const bool ok = result.accepted && result.message == "DONE";

        {
            const std::lock_guard lock(mutex_);

            QueueJobStatus& job = state_.jobs[index];

            job.state = ok ? JobState::done : JobState::failed;
            job.message = result.message;

            if (!ok && !failed_) {
                failed_ = true;

                state_.step = index;
                state_.message = job_label(index, job.name) + ": " + result.message;
            }
        }

        notify("step");

        if (!ok) {
            return false;
        }
    }

    return true;
}

CommandResult MotionQueue::run_job(
    const QueueJob& job
) {
    switch (job.type) {
    case QueueJobType::move:
        return motion_.move(job.arm, job.position, job.rotation, true, job.speed);

    case QueueJobType::path:
        return motion_.path(job.arm, job.points, job.zone, job.max_acceleration, true, job.speed);

    case QueueJobType::home:
        return motion_.home(job.arm, true, job.speed);

    case QueueJobType::gripper:
        return motion_.gripper(job.arm, job.action, true, job.grip_position, job.force);
    }

    return CommandResult{false, "Unknown job"};
}

}  // namespace gw::rws
