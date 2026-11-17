#pragma once

#include <condition_variable>
#include <cstddef>
#include <functional>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

#include "rws/motion.hpp"
#include "util/time.hpp"

namespace gw::rws {

// ================================================================
// Motion queue
// ================================================================
//
// A sequence of RWS jobs (move, path, home, gripper) that the
// gateway runs to the end on its own, so a client that dies half
// way through a pick does not leave the arm half way through it.
//
// One queue at a time, for both arms. Consecutive jobs with the
// same group form a stage that runs one lane per arm at the same
// time (each lane in order), as capture_and_detect's
// RobotClient.execute_jobs does; the next stage starts when both
// lanes are done.
//
//   idle --start--> running --> done
//                       |-----> failed     (a job failed; the rest
//                       |                   is skipped)
//                       '-----> cancelled  (cancel: after the jobs
//                                           already running)
//
// While a queue runs, start() and single jobs (admit_single) are
// refused. failed and cancelled leave the arm part way through the
// plan, so they refuse a new queue until clear(); single jobs are
// allowed then, for recovery. done refuses nothing.
//
// The queue knows nothing of the control lease: the routes decide
// who may start, cancel and clear it, and a running queue goes on
// whoever holds the lease.
//

enum class QueueJobType {
    move,
    path,
    home,
    gripper,
};

const char* job_type_name(
    QueueJobType type
);

//
// One job: the body of POST /robot/rws/move|path|home|gripper
// (wait is always true here).
//
struct QueueJob {
    std::string name;

    std::optional<std::string> group;

    QueueJobType type = QueueJobType::move;

    Arm arm = Arm::left;

    //
    // move, path and home
    //
    MotionSpeed speed;

    //
    // move
    //
    Vector3 position{};
    std::optional<Vector3> rotation;

    //
    // path
    //
    std::vector<PathPoint> points;
    double zone = 10.0;
    std::optional<double> max_acceleration;

    //
    // gripper
    //
    GripperAction action = GripperAction::open;
    std::optional<double> grip_position;
    std::optional<double> force;
};

enum class QueueState {
    idle,
    running,
    done,
    failed,
    cancelled,
};

const char* queue_state_name(
    QueueState state
);

enum class JobState {
    pending,
    running,
    done,
    failed,
    skipped,
};

const char* job_state_name(
    JobState state
);

struct QueueJobStatus {
    std::string name;

    std::optional<std::string> group;

    QueueJobType type = QueueJobType::move;

    Arm arm = Arm::left;

    JobState state = JobState::pending;

    //
    // The job's reply (DONE, ERR move failed, ...) or error.
    //
    std::optional<std::string> message;
};

struct QueueSnapshot {
    QueueState state = QueueState::idle;

    std::optional<std::string> owner;

    std::vector<QueueJobStatus> jobs;

    //
    // The first running job, or the failed one.
    //
    std::optional<std::size_t> step;

    bool cancel_requested = false;

    //
    // Why it failed or was cancelled.
    //
    std::optional<std::string> message;

    std::optional<WallTime> started_at;
    std::optional<WallTime> finished_at;
};

//
// The queue refuses (409).
//
class QueueConflict : public MotionConflict {
public:
    using MotionConflict::MotionConflict;
};

class MotionQueue {
public:
    //
    // Jobs in one queue.
    //
    static constexpr std::size_t MAX_JOBS = 64;

    //
    // Called after every change of state ("started", "step",
    // "done", "failed", "cancelling", "cancelled", "cleared"),
    // outside the lock.
    //
    using Listener = std::function<void(const std::string& event, const QueueSnapshot& snapshot)>;

    explicit MotionQueue(
        RwsMotion& motion,
        const Clock& clock = system_clock()
    );

    //
    // Waits for the worker; the gateway itself leaves with _Exit.
    //
    ~MotionQueue();

    MotionQueue(const MotionQueue&) = delete;
    MotionQueue& operator=(const MotionQueue&) = delete;

    void set_listener(
        Listener listener
    );

    //
    // Checks every job (std::invalid_argument: no jobs, too many,
    // a target outside the safe zone, a wrong gripper
    // combination), that the slot is
    // free and no single job is in progress (QueueConflict), and
    // that RAPID runs and the arms used are idle (MotionConflict,
    // RwsError), then starts the worker.
    //
    QueueSnapshot start(
        const std::string& owner,
        std::vector<QueueJob> jobs
    );

    QueueSnapshot snapshot() const;

    //
    // running: no further job is sent; the queue ends as cancelled
    // once the jobs running now have finished.
    // done / failed / cancelled: back to idle. idle: nothing.
    // Returns the state afterwards.
    //
    QueueSnapshot cancel_or_clear();

    //
    // A single job (POST /robot/rws/move ...) runs while the guard
    // lives; QueueConflict while a queue runs.
    //
    class SingleJob {
    public:
        explicit SingleJob(
            MotionQueue& queue
        );

        ~SingleJob();

        SingleJob(const SingleJob&) = delete;
        SingleJob& operator=(const SingleJob&) = delete;

    private:
        MotionQueue& queue_;
    };

    SingleJob admit_single();

    bool running() const;

    //
    // Until the queue is no longer running (tests); false on
    // timeout.
    //
    bool wait_until_finished(
        double timeout_seconds
    ) const;

private:
    void work();

    //
    // Runs the jobs of one lane (indexes into jobs_) in order;
    // false once a job failed or a cancel stopped it.
    //
    bool run_lane(
        const std::vector<std::size_t>& lane
    );

    CommandResult run_job(
        const QueueJob& job
    );

    void notify(
        const std::string& event
    );

    QueueSnapshot snapshot_locked() const;

    RwsMotion& motion_;

    const Clock& clock_;

    mutable std::mutex mutex_;

    mutable std::condition_variable changed_;

    Listener listener_;

    QueueSnapshot state_;

    std::vector<QueueJob> jobs_;

    //
    // Set when a job failed, so the other lane sends nothing more.
    //
    bool failed_ = false;

    std::size_t singles_ = 0;

    //
    // Worker threads not finished yet (the destructor waits).
    //
    std::size_t workers_ = 0;
};

}  // namespace gw::rws
