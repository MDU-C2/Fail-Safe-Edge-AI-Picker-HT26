// The RWS motion queue against the fake arm tasks: order, groups,
// failure, cancel, clear and what it refuses meanwhile. The lease
// rules are in the routes (gateway/tests/test_rws_queue.py).

#include <gtest/gtest.h>

#include <algorithm>
#include <chrono>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <vector>

#include "fake_arm_tasks.hpp"
#include "rws/motion.hpp"
#include "rws/queue.hpp"

namespace gw::rws {
namespace {

using testing::FakeArmTasks;

//
// Controller, motion and queue, destroyed in the reverse order.
//
struct Rig {
    explicit Rig(
        double job_seconds = 0.0
    )
        : controller(job_seconds),
          motion(controller, 5.0, {0.0, -500.0, 0.0}, {1000.0, 500.0, 600.0}, {}, 0.01),
          queue(motion) {}

    FakeArmTasks controller;

    RwsMotion motion;

    MotionQueue queue;
};

QueueJob move(
    const std::string& name,
    Arm arm = Arm::left,
    double x = 400.0
) {
    QueueJob job;

    job.name = name;
    job.type = QueueJobType::move;
    job.arm = arm;
    job.position = {x, 0.0, 300.0};

    return job;
}

QueueJob grip(
    const std::string& name,
    Arm arm = Arm::left
) {
    QueueJob job;

    job.name = name;
    job.type = QueueJobType::gripper;
    job.arm = arm;
    job.action = GripperAction::grip;

    return job;
}

QueueJob home(
    const std::string& name,
    Arm arm = Arm::left
) {
    QueueJob job;

    job.name = name;
    job.type = QueueJobType::home;
    job.arm = arm;

    return job;
}

std::vector<JobState> job_states(
    const QueueSnapshot& queue
) {
    std::vector<JobState> states;

    for (const QueueJobStatus& job : queue.jobs) {
        states.push_back(job.state);
    }

    return states;
}

void sleep_seconds(
    double seconds
) {
    std::this_thread::sleep_for(std::chrono::duration<double>(seconds));
}

// ================================================================
// Running
// ================================================================

TEST(MotionQueue, RunsTheJobsInOrderToDone) {
    Rig rig;

    std::mutex mutex;
    std::vector<std::string> events;

    rig.queue.set_listener([&](const std::string& event, const QueueSnapshot&) {
        const std::lock_guard lock(mutex);

        events.push_back(event);
    });

    const QueueSnapshot started = rig.queue.start("primary", {move("approach"), grip("grip"), home("home")});

    EXPECT_EQ(started.owner, "primary");
    EXPECT_EQ(started.jobs.size(), 3u);

    ASSERT_TRUE(rig.queue.wait_until_finished(5.0));

    const QueueSnapshot queue = rig.queue.snapshot();

    EXPECT_EQ(queue.state, QueueState::done);
    EXPECT_EQ(job_states(queue), std::vector<JobState>(3, JobState::done));
    EXPECT_EQ(queue.jobs[1].message, "DONE");
    EXPECT_FALSE(queue.step.has_value());
    EXPECT_TRUE(queue.finished_at.has_value());

    //
    // The jobs reached the arm in order: target, grip, home.
    //
    std::vector<std::string> order;

    for (const auto& [task, name, value] : rig.controller.written()) {
        if (name == "target_pos_left") {
            order.push_back("move");
        } else if (name == "grip_cmd_left" && value == "\"GRIP\"") {
            order.push_back("grip");
        } else if (name == "go_home_left" && value == "TRUE") {
            order.push_back("home");
        }
    }

    EXPECT_EQ(order, (std::vector<std::string>{"move", "grip", "home"}));

    //
    // done refuses nothing.
    //
    rig.queue.start("secondary", {home("home")});

    ASSERT_TRUE(rig.queue.wait_until_finished(5.0));

    const std::lock_guard lock(mutex);

    EXPECT_EQ(events.front(), "started");
    EXPECT_EQ(events.back(), "done");
    EXPECT_EQ(std::count(events.begin(), events.end(), "step"), 4);
}

TEST(MotionQueue, AGroupRunsBothArmsAtOnce) {
    Rig rig(0.3);

    QueueJob left = move("left", Arm::left);
    QueueJob right = move("right", Arm::right);

    left.group = "together";
    right.group = "together";

    const auto start = std::chrono::steady_clock::now();

    rig.queue.start("primary", {left, right, home("after")});

    ASSERT_TRUE(rig.queue.wait_until_finished(5.0));

    const double elapsed = std::chrono::duration<double>(std::chrono::steady_clock::now() - start).count();

    EXPECT_EQ(rig.queue.snapshot().state, QueueState::done);

    //
    // Two 0.3 s jobs at once, then one: well under 0.9 s.
    //
    EXPECT_LT(elapsed, 0.8);
    EXPECT_GE(elapsed, 0.6);
}

// ================================================================
// Failure, cancel, clear
// ================================================================

TEST(MotionQueue, AFailedJobStopsTheQueueUntilCleared) {
    Rig rig;

    rig.queue.start("primary", {move("approach"), move("unreachable", Arm::left, 900.0), home("home")});

    ASSERT_TRUE(rig.queue.wait_until_finished(5.0));

    const QueueSnapshot queue = rig.queue.snapshot();

    EXPECT_EQ(queue.state, QueueState::failed);
    EXPECT_EQ(queue.step, 1u);
    EXPECT_EQ(
        job_states(queue),
        (std::vector<JobState>{JobState::done, JobState::failed, JobState::skipped})
    );
    EXPECT_EQ(queue.jobs[1].message, "ERR move failed");
    EXPECT_NE(queue.message->find("Job 2 (unreachable)"), std::string::npos);

    //
    // No new queue until it is cleared; single jobs (recovery) are
    // allowed.
    //
    EXPECT_THROW(rig.queue.start("secondary", {home("home")}), QueueConflict);
    EXPECT_NO_THROW(rig.queue.admit_single());

    EXPECT_EQ(rig.queue.cancel_or_clear().state, QueueState::idle);
    EXPECT_TRUE(rig.queue.snapshot().jobs.empty());

    rig.queue.start("secondary", {home("home")});

    ASSERT_TRUE(rig.queue.wait_until_finished(5.0));

    EXPECT_EQ(rig.queue.snapshot().state, QueueState::done);
}

TEST(MotionQueue, AFailureInOneLaneStopsTheOther) {
    Rig rig(0.2);

    //
    // Fails at once (the program has no grip_pos_left), while the
    // right lane starts.
    //
    rig.controller.remove_symbol("grip_pos_left");

    QueueJob left = grip("left fails", Arm::left);

    left.action = GripperAction::move;
    left.grip_position = 10.0;

    QueueJob right_first = move("right 1", Arm::right, 400.0);
    QueueJob right_second = move("right 2", Arm::right, 450.0);

    left.group = right_first.group = right_second.group = "g";

    rig.queue.start("primary", {left, right_first, right_second});

    ASSERT_TRUE(rig.queue.wait_until_finished(5.0));

    const QueueSnapshot queue = rig.queue.snapshot();

    EXPECT_EQ(queue.state, QueueState::failed);
    EXPECT_EQ(queue.step, 0u);

    //
    // The right arm's first job finishes if it was already sent
    // (else it is skipped too); its next is never sent.
    //
    const std::vector<JobState> states = job_states(queue);

    EXPECT_EQ(states[0], JobState::failed);
    EXPECT_TRUE(states[1] == JobState::done || states[1] == JobState::skipped);
    EXPECT_EQ(states[2], JobState::skipped);

    for (const auto& [task, name, value] : rig.controller.written()) {
        EXPECT_FALSE(name == "target_pos_right" && value.rfind("[450", 0) == 0);
    }
}

TEST(MotionQueue, CancelStopsAfterTheRunningJob) {
    Rig rig(0.3);

    rig.queue.start("primary", {move("first"), grip("second"), home("third")});

    sleep_seconds(0.1);

    const QueueSnapshot cancelling = rig.queue.cancel_or_clear();

    EXPECT_EQ(cancelling.state, QueueState::running);
    EXPECT_TRUE(cancelling.cancel_requested);

    ASSERT_TRUE(rig.queue.wait_until_finished(5.0));

    const QueueSnapshot queue = rig.queue.snapshot();

    EXPECT_EQ(queue.state, QueueState::cancelled);
    EXPECT_EQ(queue.step, 1u);
    EXPECT_EQ(
        job_states(queue),
        (std::vector<JobState>{JobState::done, JobState::skipped, JobState::skipped})
    );

    //
    // The running job was not interrupted; the rest was never sent.
    //
    EXPECT_EQ(rig.controller.symbol("move_request_left"), "FALSE");
    EXPECT_EQ(rig.controller.symbol("go_home_left"), "FALSE");

    EXPECT_THROW(rig.queue.start("secondary", {home("home")}), QueueConflict);

    EXPECT_EQ(rig.queue.cancel_or_clear().state, QueueState::idle);

    //
    // Idle: nothing to cancel or clear.
    //
    EXPECT_EQ(rig.queue.cancel_or_clear().state, QueueState::idle);
}

// ================================================================
// What a running queue refuses, and what refuses a queue
// ================================================================

TEST(MotionQueue, WhileRunningRefusesSingleJobsAndAnotherQueue) {
    Rig rig(0.3);

    rig.queue.start("primary", {move("first"), move("second", Arm::right)});

    EXPECT_TRUE(rig.queue.running());
    EXPECT_THROW(rig.queue.admit_single(), QueueConflict);
    EXPECT_THROW(rig.queue.start("admin", {home("home")}), QueueConflict);

    ASSERT_TRUE(rig.queue.wait_until_finished(5.0));

    EXPECT_FALSE(rig.queue.running());
    EXPECT_NO_THROW(rig.queue.admit_single());
}

TEST(MotionQueue, ASingleJobInProgressRefusesAQueue) {
    Rig rig;

    {
        const auto admitted = rig.queue.admit_single();

        EXPECT_THROW(rig.queue.start("primary", {home("home")}), QueueConflict);
    }

    rig.queue.start("primary", {home("home")});

    ASSERT_TRUE(rig.queue.wait_until_finished(5.0));
}

TEST(MotionQueue, RefusesABadQueueBeforeAnythingIsWritten) {
    Rig rig;

    QueueJob bad_gripper = grip("bad");

    bad_gripper.action = GripperAction::move;

    QueueJob empty_path;

    empty_path.name = "path";
    empty_path.type = QueueJobType::path;

    EXPECT_THROW(rig.queue.start("primary", {}), std::invalid_argument);
    EXPECT_THROW(rig.queue.start("primary", std::vector<QueueJob>(MotionQueue::MAX_JOBS + 1, home("home"))), std::invalid_argument);
    EXPECT_THROW(rig.queue.start("primary", {move("ok"), move("outside", Arm::left, 2000.0)}), std::invalid_argument);
    EXPECT_THROW(rig.queue.start("primary", {bad_gripper}), std::invalid_argument);
    EXPECT_THROW(rig.queue.start("primary", {empty_path}), std::invalid_argument);

    EXPECT_TRUE(rig.controller.written().empty());
    EXPECT_EQ(rig.queue.snapshot().state, QueueState::idle);
}

TEST(MotionQueue, NeedsRapidRunningAndIdleArms) {
    Rig rig;

    rig.controller.set_state("stopped");

    EXPECT_THROW(rig.queue.start("primary", {home("home")}), MotionConflict);
    EXPECT_EQ(rig.queue.snapshot().state, QueueState::idle);

    rig.controller.set_state("running");
    rig.controller.set_symbol("move_request_right", "TRUE");

    //
    // Only the arms the queue uses must be idle.
    //
    EXPECT_THROW(rig.queue.start("primary", {home("home", Arm::right)}), MotionConflict);

    rig.queue.start("primary", {home("home", Arm::left)});

    ASSERT_TRUE(rig.queue.wait_until_finished(5.0));

    EXPECT_EQ(rig.queue.snapshot().state, QueueState::done);
    EXPECT_TRUE(rig.controller.written().size() > 0);
}

TEST(MotionQueue, RapidStoppingFailsTheQueue) {
    Rig rig(0.5);

    rig.queue.start("primary", {move("first"), home("second")});

    sleep_seconds(0.1);

    rig.controller.set_state("stopped");

    ASSERT_TRUE(rig.queue.wait_until_finished(5.0));

    const QueueSnapshot queue = rig.queue.snapshot();

    EXPECT_EQ(queue.state, QueueState::failed);
    EXPECT_EQ(queue.step, 0u);
    EXPECT_NE(queue.jobs[0].message->find("RAPID stopped"), std::string::npos);
    EXPECT_EQ(queue.jobs[1].state, JobState::skipped);
}

}  // namespace
}  // namespace gw::rws
