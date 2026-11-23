// The RWS heartbeat that feeds the arm tasks' gateway watchdog,
// against the fake arm tasks.

#include <gtest/gtest.h>

#include <string>
#include <vector>

#include "fake_arm_tasks.hpp"
#include "rws/heartbeat.hpp"

namespace gw::rws {
namespace {

using testing::FakeArmTasks;

void add_watchdog(
    FakeArmTasks& controller
) {
    for (const char* name : {"gw_heartbeat", "gw_session", "gw_timeout"}) {
        controller.set_symbol(name, "0");
    }

    controller.set_symbol("auto_home_left", "FALSE");
    controller.set_symbol("auto_home_right", "FALSE");
}

int count_writes(
    FakeArmTasks& controller,
    const std::string& name
) {
    int count = 0;

    for (const auto& [task, written, value] : controller.written()) {
        count += written == name;
    }

    return count;
}

TEST(RwsHeartbeat, WritesTimeoutSessionOnceAndCountsUp) {
    FakeArmTasks controller;
    add_watchdog(controller);

    RwsHeartbeat heartbeat(controller, 5.0);

    heartbeat.beat();
    heartbeat.beat();
    heartbeat.beat();

    EXPECT_EQ(controller.symbol("gw_timeout"), "5");
    EXPECT_EQ(controller.symbol("gw_session"), std::to_string(heartbeat.session()));
    EXPECT_EQ(controller.symbol("gw_heartbeat"), "3");

    EXPECT_EQ(count_writes(controller, "gw_session"), 1);
    EXPECT_EQ(count_writes(controller, "gw_timeout"), 3);

    for (const auto& [task, name, value] : controller.written()) {
        EXPECT_EQ(task, "T_ROB_L");
    }

    const auto state = heartbeat.state();

    EXPECT_TRUE(state.enabled);
    EXPECT_TRUE(state.connected);
    EXPECT_FALSE(state.error);
    EXPECT_EQ(state.auto_home[0], false);
    EXPECT_EQ(state.auto_home[1], false);
}

TEST(RwsHeartbeat, SessionDiffersBetweenGatewayStarts) {
    FakeArmTasks controller;

    //
    // Random, 1 .. 8000000: two in a row are the same once in 8
    // million.
    //
    EXPECT_NE(RwsHeartbeat(controller, 5.0).session(), RwsHeartbeat(controller, 5.0).session());
}

TEST(RwsHeartbeat, ProgramWithoutWatchdogIsReportedAndTriedAgain) {
    FakeArmTasks controller;

    RwsHeartbeat heartbeat(controller, 5.0);

    heartbeat.beat();

    auto state = heartbeat.state();

    EXPECT_FALSE(state.connected);
    ASSERT_TRUE(state.error);
    EXPECT_NE(state.error->find("watchdog"), std::string::npos);

    //
    // rapid/ uploaded again.
    //
    add_watchdog(controller);

    heartbeat.beat();

    state = heartbeat.state();

    EXPECT_TRUE(state.connected);
    EXPECT_FALSE(state.error);
    EXPECT_EQ(controller.symbol("gw_heartbeat"), "2");
}

TEST(RwsHeartbeat, ReportsAutoHomeOnceWhenItTurnsTrue) {
    FakeArmTasks controller;
    add_watchdog(controller);

    std::vector<Arm> reported;

    RwsHeartbeat heartbeat(controller, 5.0, [&](Arm arm) { reported.push_back(arm); });

    heartbeat.beat();

    EXPECT_TRUE(reported.empty());

    controller.set_symbol("auto_home_right", "TRUE");

    heartbeat.beat();
    heartbeat.beat();

    ASSERT_EQ(reported.size(), 1u);
    EXPECT_EQ(reported[0], Arm::right);
    EXPECT_EQ(heartbeat.state().auto_home[1], true);

    //
    // The next job clears it; going home again is reported again.
    //
    controller.set_symbol("auto_home_right", "FALSE");
    heartbeat.beat();
    controller.set_symbol("auto_home_right", "TRUE");
    heartbeat.beat();

    EXPECT_EQ(reported.size(), 2u);
}

TEST(RwsHeartbeat, ZeroTimeoutWritesNothing) {
    FakeArmTasks controller;
    add_watchdog(controller);

    RwsHeartbeat heartbeat(controller, 0.0);

    heartbeat.start();
    heartbeat.stop();

    EXPECT_TRUE(controller.written().empty());
    EXPECT_FALSE(heartbeat.state().enabled);
}

TEST(RwsHeartbeat, ThreadBeatsEveryInterval) {
    FakeArmTasks controller;
    add_watchdog(controller);

    RwsHeartbeat heartbeat(controller, 5.0, {}, 0.02);

    heartbeat.start();
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    heartbeat.stop();

    EXPECT_GE(std::stoi(controller.symbol("gw_heartbeat")), 3);
}

}  // namespace
}  // namespace gw::rws
