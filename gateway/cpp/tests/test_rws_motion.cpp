// RWS motion handshake and RAPID status against fake controllers.
// A port of the unit tests in gateway/tests/test_rws.py (marked
// python_internals there), plus the RAPID source checks.

#include <gtest/gtest.h>

#include <algorithm>
#include <chrono>
#include <map>
#include <mutex>
#include <thread>
#include <tuple>
#include <vector>

#include "http/message.hpp"
#include "rws/errors.hpp"
#include "rws/http_errors.hpp"
#include "rws/motion.hpp"
#include "rws/rapid.hpp"

namespace gw::rws {
namespace {

using Write = std::tuple<std::string, std::string, std::string>;

//
// The variables of rapid/SharedVars.mod in the two arm tasks. An arm
// finishes a job job_seconds after move_request is set; a target
// with x >= 800 is out of reach.
//
class FakeArmTasks : public Controller {
public:
    explicit FakeArmTasks(
        double job_seconds = 0.0
    )
        : job_seconds_(job_seconds) {
        for (const char* arm : {"left", "right"}) {
            for (const auto& [name, value] : std::vector<std::pair<std::string, std::string>>{
                     {"move_request", "FALSE"},
                     {"move_ok", "FALSE"},
                     {"go_home", "FALSE"},
                     {"use_rot", "FALSE"},
                     {"grip_cmd", "\"\""},
                     {"target_pos", "[0,0,0]"},
                     {"target_rot", "[1,0,0,0]"},
                     {"move_speed", "[50,500,5000,1000]"},
                     {"move_acc", "100"},
                     {"move_ramp", "100"},
                     {"grip_pos", "25"},
                     {"grip_force", "20"},
                 }) {
                symbols_[name + "_" + arm] = value;
            }
        }
    }

    ~FakeArmTasks() override {
        for (auto& timer : timers_) {
            timer.join();
        }
    }

    std::string execution_state() override {
        const std::lock_guard lock(mutex_);

        return state;
    }

    std::string controller_state() override {
        return "motoron";
    }

    std::string operation_mode() override {
        return "AUTO";
    }

    nlohmann::json tasks() override {
        return nlohmann::json::array();
    }

    std::vector<std::string> program_modules(
        const std::string&
    ) override {
        return {};
    }

    std::string read_symbol(
        const std::string&,
        const std::string& name
    ) override {
        const std::lock_guard lock(mutex_);

        const auto it = symbols_.find(name);

        if (it == symbols_.end()) {
            throw RwsError(400, "Symbol not found");
        }

        return it->second;
    }

    void write_symbol(
        const std::string& task,
        const std::string& name,
        const std::string& value
    ) override {
        const std::lock_guard lock(mutex_);

        writes.emplace_back(task, name, value);
        symbols_[name] = value;

        if (name.rfind("move_request_", 0) == 0 && value == "TRUE") {
            const std::string arm = name.substr(std::string("move_request_").size());

            timers_.emplace_back([this, arm]() {
                std::this_thread::sleep_for(std::chrono::duration<double>(job_seconds_));
                finish(arm);
            });
        }
    }

    std::string symbol(
        const std::string& name
    ) {
        const std::lock_guard lock(mutex_);

        return symbols_.at(name);
    }

    void set_symbol(
        const std::string& name,
        const std::string& value
    ) {
        const std::lock_guard lock(mutex_);

        symbols_[name] = value;
    }

    void remove_symbol(
        const std::string& name
    ) {
        const std::lock_guard lock(mutex_);

        symbols_.erase(name);
    }

    void set_state(
        const std::string& value
    ) {
        const std::lock_guard lock(mutex_);

        state = value;
    }

    std::vector<Write> written() {
        const std::lock_guard lock(mutex_);

        return writes;
    }

    void clear_writes() {
        const std::lock_guard lock(mutex_);

        writes.clear();
    }

    //
    // name -> (task, value) of the last write.
    //
    std::map<std::string, std::pair<std::string, std::string>> last_writes() {
        std::map<std::string, std::pair<std::string, std::string>> result;

        for (const auto& [task, name, value] : written()) {
            result[name] = {task, value};
        }

        return result;
    }

private:
    void finish(
        const std::string& arm
    ) {
        const std::lock_guard lock(mutex_);

        const std::string target = symbols_["target_pos_" + arm];
        const double x = std::stod(target.substr(1, target.find(',') - 1));

        const bool ok =
            symbols_["go_home_" + arm] == "TRUE"
            || symbols_["grip_cmd_" + arm] != "\"\""
            || x < 800;

        symbols_["move_ok_" + arm] = ok ? "TRUE" : "FALSE";
        symbols_["grip_cmd_" + arm] = "\"\"";
        symbols_["move_request_" + arm] = "FALSE";
    }

    double job_seconds_;

    std::mutex mutex_;

    std::string state = "running";

    std::vector<Write> writes;

    std::map<std::string, std::string> symbols_;

    std::vector<std::thread> timers_;
};

RwsMotion motion(
    FakeArmTasks& controller
) {
    return RwsMotion(controller, 5.0, {0.0, -500.0, 0.0}, {1000.0, 500.0, 600.0});
}

bool any_write_starting_with(
    const std::vector<Write>& writes,
    const std::string& prefix
) {
    for (const auto& [task, name, value] : writes) {
        if (name.rfind(prefix, 0) == 0) {
            return true;
        }
    }

    return false;
}

// ================================================================
// OrientZYX
// ================================================================

TEST(OrientZyx, MatchesRapid) {
    const std::vector<std::pair<Vector3, Quaternion>> cases = {
        {{0, 0, 0}, {1.0, 0.0, 0.0, 0.0}},
        {{90, 0, 90}, {0.5, 0.5, 0.5, 0.5}},
        //
        // Read from OrientZYX(60, 20, 30) on the virtual controller.
        //
        {{60, 20, 30}, {0.846279, 0.136873, 0.272703, 0.436703}},
    };

    for (const auto& [angles, expected] : cases) {
        const Quaternion q = orient_zyx(angles[0], angles[1], angles[2]);

        for (int i = 0; i < 4; ++i) {
            EXPECT_NEAR(q[i], expected[i], 1e-6);
        }
    }
}

// ================================================================
// RAPID status
// ================================================================

//
// The task list of the real YuMi: two background tasks without an
// active field and a non-motion communication task.
//
class FiveTaskController : public FakeArmTasks {
public:
    std::string controller_state() override {
        return "motoron";
    }

    std::string operation_mode() override {
        return "MANR";
    }

    nlohmann::json tasks() override {
        return nlohmann::json::parse(R"([
            {"name": "T_ROB_R", "taskstate": "init", "excstate": "star", "active": "On"},
            {"name": "T_ROB_L", "taskstate": "init", "excstate": "star", "active": "On"},
            {"name": "YuMiLibBG_R", "taskstate": "init", "excstate": "star"},
            {"name": "YuMiLibBG_L", "taskstate": "init", "excstate": "star"},
            {"name": "communication", "taskstate": "init", "excstate": "star", "active": "Off"}
        ])");
    }

    std::vector<std::string> program_modules(
        const std::string&
    ) override {
        return {"SharedVars"};
    }
};

TEST(RapidStatus, HandlesBackgroundTasks) {
    FiveTaskController controller;

    const http::Json status = read_status(controller);

    std::map<std::string, bool> active;

    for (const auto& task : status["tasks"]) {
        active[task["name"]] = task["active"];
    }

    EXPECT_EQ(
        active,
        (std::map<std::string, bool>{
            {"T_ROB_R", true},
            {"T_ROB_L", true},
            {"YuMiLibBG_R", true},
            {"YuMiLibBG_L", true},
            {"communication", false},
        })
    );

    EXPECT_EQ(status["execution_state"], "running");
    EXPECT_EQ(status["operation_mode"], "MANR");
    EXPECT_EQ(status["tasks"][0]["modules"], http::Json::array({"SharedVars"}));
}

TEST(RapidStatus, StoppedTaskIsRequiredForModuleChanges) {
    FiveTaskController controller;

    try {
        require_stopped_task(controller, "T_ROB_X");
        FAIL() << "expected 404";
    } catch (const http::HttpError& error) {
        EXPECT_EQ(error.status, 404);
        EXPECT_EQ(
            error.detail,
            "Unknown task T_ROB_X, the controller has T_ROB_R, T_ROB_L, YuMiLibBG_R, YuMiLibBG_L, communication"
        );
    }

    try {
        require_stopped_task(controller, "T_ROB_L");
        FAIL() << "expected 409";
    } catch (const http::HttpError& error) {
        EXPECT_EQ(error.status, 409);
    }

    controller.set_state("stopped");

    EXPECT_NO_THROW(require_stopped_task(controller, "T_ROB_L"));
}

// ================================================================
// Motion handshake
// ================================================================

TEST(RwsMotion, WritesTheArmsVariables) {
    FakeArmTasks controller;

    const CommandResult result = motion(controller).move(Arm::right, {420.0, -150.0, 300.0}, Vector3{180.0, 0.0, 0.0});

    EXPECT_TRUE(result.accepted);
    EXPECT_EQ(result.message, "DONE");

    auto written = controller.last_writes();

    EXPECT_EQ(written["target_pos_right"], (std::pair<std::string, std::string>{"T_ROB_R", "[420.000000,-150.000000,300.000000]"}));
    EXPECT_EQ(written["use_rot_right"].second, "TRUE");
    EXPECT_EQ(written["go_home_right"].second, "FALSE");
    EXPECT_EQ(written.count("target_rot_right"), 1u);

    //
    // move_request is set last, after the job is written.
    //
    EXPECT_EQ(controller.written().back(), (Write{"T_ROB_R", "move_request_right", "TRUE"}));

    for (const auto& [task, name, value] : controller.written()) {
        EXPECT_EQ(name.find("_left"), std::string::npos) << name;
    }

    //
    // No speed given: the default (v50, AccSet 100, 100).
    //
    EXPECT_EQ(written["move_speed_right"].second, "[50.000000,500.000000,5000.000000,1000.000000]");
    EXPECT_EQ(written["move_acc_right"].second, "100.000000");
    EXPECT_EQ(written["move_ramp_right"].second, "100.000000");
}

TEST(RwsMotion, WritesTheSpeed) {
    FakeArmTasks controller;
    RwsMotion arms = motion(controller);

    EXPECT_EQ(arms.move(Arm::left, {400.0, 150.0, 320.0}, std::nullopt, true, MotionSpeed{200.0, 50.0, 30.0}).message, "DONE");

    EXPECT_EQ(controller.symbol("move_speed_left"), "[200.000000,500.000000,5000.000000,1000.000000]");
    EXPECT_EQ(controller.symbol("move_acc_left"), "50.000000");
    EXPECT_EQ(controller.symbol("move_ramp_left"), "30.000000");

    //
    // Every job writes its own speed, so nothing carries over.
    //
    EXPECT_EQ(arms.home(Arm::left).message, "DONE");

    EXPECT_EQ(controller.symbol("move_speed_left"), "[50.000000,500.000000,5000.000000,1000.000000]");
    EXPECT_EQ(controller.symbol("move_acc_left"), "100.000000");

    EXPECT_EQ(arms.home(Arm::right, true, MotionSpeed{120.0, 100.0, 100.0}).message, "DONE");

    EXPECT_EQ(controller.symbol("move_speed_right").rfind("[120.000000,", 0), 0u);

    //
    // The gripper does not touch the speed.
    //
    controller.clear_writes();

    EXPECT_EQ(arms.gripper(Arm::left, GripperAction::grip).message, "DONE");

    EXPECT_FALSE(any_write_starting_with(controller.written(), "move_speed"));
}

TEST(RwsMotion, RefusesAProgramWithoutSpeedVariables) {
    FakeArmTasks controller;

    controller.remove_symbol("move_speed_left");

    try {
        motion(controller).move(Arm::left, {400.0, 150.0, 320.0}, std::nullopt);
        FAIL() << "expected MotionConflict";
    } catch (const MotionConflict& error) {
        EXPECT_NE(std::string(error.what()).find("no speed variables"), std::string::npos);
    }

    EXPECT_TRUE(controller.written().empty());

    //
    // The gripper does not need them.
    //
    EXPECT_EQ(motion(controller).gripper(Arm::left, GripperAction::open).message, "DONE");
}

TEST(RwsMotion, HomeAndGripper) {
    FakeArmTasks controller;
    RwsMotion arms = motion(controller);

    EXPECT_EQ(arms.home(Arm::left).message, "DONE");
    EXPECT_EQ(controller.symbol("go_home_left"), "TRUE");

    EXPECT_EQ(arms.gripper(Arm::left, GripperAction::grip).message, "DONE");

    const auto writes = controller.written();

    EXPECT_NE(std::find(writes.begin(), writes.end(), Write{"T_ROB_L", "grip_cmd_left", "\"GRIP\""}), writes.end());
    EXPECT_EQ(controller.symbol("go_home_left"), "FALSE");

    EXPECT_EQ(arms.gripper(Arm::right, GripperAction::open).message, "DONE");

    const auto more = controller.written();

    EXPECT_NE(std::find(more.begin(), more.end(), Write{"T_ROB_R", "grip_cmd_right", "\"OPEN\""}), more.end());
}

TEST(RwsMotion, GripperPositionAndForce) {
    FakeArmTasks controller;
    RwsMotion arms = motion(controller);

    EXPECT_EQ(arms.gripper(Arm::left, GripperAction::move, true, 12.5).message, "DONE");

    const auto writes = controller.written();

    ASSERT_GE(writes.size(), 3u);
    EXPECT_EQ(
        std::vector<Write>(writes.end() - 3, writes.end()),
        (std::vector<Write>{
            {"T_ROB_L", "grip_cmd_left", "\"MOVE\""},
            {"T_ROB_L", "grip_pos_left", "12.500000"},
            {"T_ROB_L", "move_request_left", "TRUE"},
        })
    );

    EXPECT_EQ(arms.gripper(Arm::right, GripperAction::grip, true, std::nullopt, 4.5).message, "DONE");
    EXPECT_EQ(controller.symbol("grip_force_right"), "4.500000");

    //
    // Without a force: the SmartGripper default, so an earlier
    // gentle grip does not carry over.
    //
    EXPECT_EQ(arms.gripper(Arm::right, GripperAction::grip).message, "DONE");
    EXPECT_EQ(controller.symbol("grip_force_right"), "20.000000");

    //
    // open does not touch the position or the force.
    //
    controller.clear_writes();

    EXPECT_EQ(arms.gripper(Arm::right, GripperAction::open).message, "DONE");

    EXPECT_FALSE(any_write_starting_with(controller.written(), "grip_force"));
    EXPECT_FALSE(any_write_starting_with(controller.written(), "grip_pos"));

    EXPECT_THROW(arms.gripper(Arm::left, GripperAction::move), std::invalid_argument);
    EXPECT_THROW(arms.gripper(Arm::left, GripperAction::open, true, 10.0), std::invalid_argument);
    EXPECT_THROW(arms.gripper(Arm::left, GripperAction::open, true, std::nullopt, 10.0), std::invalid_argument);
}

TEST(RwsMotion, GripperOnAProgramWithoutPositionAndForce) {
    FakeArmTasks controller;

    controller.remove_symbol("grip_pos_left");
    controller.remove_symbol("grip_force_left");

    RwsMotion arms = motion(controller);

    //
    // An older arm loop would take MOVE for an arm move to
    // target_pos, so nothing is written.
    //
    try {
        arms.gripper(Arm::left, GripperAction::move, true, 10.0);
        FAIL();
    } catch (const MotionConflict& error) {
        EXPECT_NE(std::string(error.what()).find("no gripper position variables"), std::string::npos);
    }

    try {
        arms.gripper(Arm::left, GripperAction::grip, true, std::nullopt, 5.0);
        FAIL();
    } catch (const MotionConflict& error) {
        EXPECT_NE(std::string(error.what()).find("no gripper force variables"), std::string::npos);
    }

    EXPECT_TRUE(controller.written().empty());

    //
    // A plain grip or open works as before (g_GripIn's own default
    // force).
    //
    EXPECT_EQ(arms.gripper(Arm::left, GripperAction::grip).message, "DONE");
    EXPECT_EQ(arms.gripper(Arm::left, GripperAction::open).message, "DONE");

    EXPECT_FALSE(any_write_starting_with(controller.written(), "grip_force"));
}

TEST(RwsMotion, RefusesTargetsOutsideTheSafeZone) {
    FakeArmTasks controller;

    const CommandResult result = motion(controller).move(Arm::left, {300.0, 150.0, 700.0}, std::nullopt);

    EXPECT_FALSE(result.accepted);
    EXPECT_EQ(result.message, "ERR outside safe zone");
    EXPECT_TRUE(controller.written().empty());
}

TEST(RwsMotion, ReportsAFailedMove) {
    FakeArmTasks controller;

    const CommandResult result = motion(controller).move(Arm::left, {900.0, 0.0, 300.0}, std::nullopt);

    EXPECT_FALSE(result.accepted);
    EXPECT_EQ(result.message, "ERR move failed");
}

TEST(RwsMotion, ArmsAreIndependent) {
    FakeArmTasks controller(0.5);
    RwsMotion arms = motion(controller);

    CommandResult left_result;

    std::thread left([&]() { left_result = arms.home(Arm::left); });

    std::this_thread::sleep_for(std::chrono::milliseconds(100));

    EXPECT_TRUE(arms.is_moving());

    try {
        arms.home(Arm::left);
        FAIL();
    } catch (const MotionConflict& error) {
        EXPECT_STREQ(error.what(), "A left arm job is already in progress");
    }

    const auto started = std::chrono::steady_clock::now();

    EXPECT_EQ(arms.home(Arm::right).message, "DONE");

    left.join();

    EXPECT_EQ(left_result.message, "DONE");
    EXPECT_LT(std::chrono::steady_clock::now() - started, std::chrono::seconds(1));
    EXPECT_FALSE(arms.is_moving());
}

TEST(RwsMotion, RefusesABusyOrMissingProgram) {
    FakeArmTasks controller;

    controller.set_symbol("move_request_left", "TRUE");

    try {
        motion(controller).home(Arm::left);
        FAIL();
    } catch (const MotionConflict& error) {
        EXPECT_STREQ(error.what(), "The left arm is busy with an earlier job");
    }

    controller.set_state("stopped");

    try {
        motion(controller).home(Arm::right);
        FAIL();
    } catch (const MotionConflict& error) {
        EXPECT_STREQ(error.what(), "RAPID is not running");
    }

    controller.set_state("running");
    controller.remove_symbol("move_request_right");

    try {
        motion(controller).home(Arm::right);
        FAIL();
    } catch (const MotionConflict& error) {
        EXPECT_STREQ(error.what(), "The motion program is not loaded in T_ROB_R (no move_request_right variable)");
    }
}

TEST(RwsMotion, CanStartWithoutWaiting) {
    FakeArmTasks controller(0.3);
    RwsMotion arms = motion(controller);

    EXPECT_EQ(arms.status(Arm::left).message, "ERR move failed");

    const CommandResult started = arms.move(Arm::left, {400.0, 150.0, 320.0}, std::nullopt, false);

    EXPECT_TRUE(started.accepted);
    EXPECT_EQ(started.message, "STARTED");
    EXPECT_FALSE(arms.is_moving());

    const CommandResult busy = arms.status(Arm::left);

    EXPECT_TRUE(busy.accepted);
    EXPECT_EQ(busy.message, "BUSY");

    //
    // Like CommServer's ERR busy.
    //
    EXPECT_THROW(arms.gripper(Arm::left, GripperAction::grip, false), MotionConflict);

    //
    // The other arm is free meanwhile.
    //
    EXPECT_EQ(arms.home(Arm::right, false).message, "STARTED");

    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(5);

    while (arms.status(Arm::left).message == "BUSY") {
        ASSERT_LT(std::chrono::steady_clock::now(), deadline);
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }

    EXPECT_EQ(arms.status(Arm::left).message, "DONE");

    while (arms.status(Arm::right).message == "BUSY") {
        ASSERT_LT(std::chrono::steady_clock::now(), deadline);
        std::this_thread::sleep_for(std::chrono::milliseconds(50));
    }

    EXPECT_EQ(arms.status(Arm::right).message, "DONE");
}

TEST(RwsMotion, StatusReportsAFailedJob) {
    FakeArmTasks controller;
    RwsMotion arms = motion(controller);

    EXPECT_EQ(arms.move(Arm::left, {900.0, 0.0, 300.0}, std::nullopt, false).message, "STARTED");

    std::this_thread::sleep_for(std::chrono::milliseconds(200));

    const CommandResult status = arms.status(Arm::left);

    EXPECT_FALSE(status.accepted);
    EXPECT_EQ(status.message, "ERR move failed");

    controller.remove_symbol("move_request_left");

    EXPECT_THROW(arms.status(Arm::left), MotionConflict);
}

// ================================================================
// Not in the Python tests
// ================================================================

TEST(RwsMotion, TimesOutWhenTheArmNeverFinishes) {
    FakeArmTasks controller(1.0);

    RwsMotion arms(controller, 0.2, {0.0, -500.0, 0.0}, {1000.0, 500.0, 600.0});

    try {
        arms.home(Arm::left);
        FAIL();
    } catch (const MotionTimeout& error) {
        EXPECT_STREQ(error.what(), "The left arm did not finish within 0.2 s");
    }

    //
    // The arm is still busy until RAPID clears move_request.
    //
    EXPECT_THROW(arms.home(Arm::left), MotionConflict);
}

TEST(RwsMotion, RapidStoppingDuringAMoveIsAConflict) {
    FakeArmTasks controller(1.0);
    RwsMotion arms = motion(controller);

    std::thread stopper([&]() {
        std::this_thread::sleep_for(std::chrono::milliseconds(150));
        controller.set_state("stopped");
    });

    try {
        arms.home(Arm::left);
        FAIL();
    } catch (const MotionConflict& error) {
        EXPECT_STREQ(error.what(), "RAPID stopped before the move finished");
    }

    stopper.join();
}

// ================================================================
// RAPID sources
// ================================================================

TEST(RapidSource, FindsTheDeclaredModule) {
    EXPECT_EQ(declared_module("MODULE GatewayTest\nENDMODULE\n"), "GatewayTest");
    EXPECT_EQ(declared_module("%%%\n  VERSION:1\n%%%\n  module Lower_2(SYSMODULE)\n"), "Lower_2");
    EXPECT_EQ(declared_module("\n\n   MODULE   Spaced\n"), "Spaced");
    EXPECT_EQ(declared_module("! MODULE Comment\nPROC main()\nENDPROC\n"), std::nullopt);
    EXPECT_EQ(declared_module("MODULEX Name\n"), std::nullopt);
    EXPECT_EQ(declared_module("MODULE 1Name\n"), std::nullopt);
    EXPECT_EQ(declared_module(""), std::nullopt);
}

TEST(RapidSource, MustDeclareTheModuleAndFitLatin1) {
    EXPECT_NO_THROW(check_source("GatewayTest", "MODULE gatewaytest\nENDMODULE\n"));
    EXPECT_NO_THROW(check_source("GatewayTest", "MODULE GatewayTest\n! \xc3\xa4\xc3\xb6\nENDMODULE\n"));

    EXPECT_THROW(check_source("Other", "MODULE GatewayTest\nENDMODULE\n"), http::HttpError);

    try {
        check_source("GatewayTest", "MODULE GatewayTest\n! \xe2\x98\x83\xe2\x98\x83 x \xe2\x98\x83\n");
        FAIL();
    } catch (const http::HttpError& error) {
        EXPECT_EQ(error.status, 422);
        EXPECT_EQ(
            error.detail,
            "Source contains characters the controller cannot store (ISO-8859-1): '\xe2\x98\x83\xe2\x98\x83'"
        );
    }
}

TEST(RapidSource, ConvertsToLatin1) {
    const Latin1 latin1 = to_latin1("a\xc3\xa4z");

    EXPECT_EQ(latin1.bytes, std::string("a\xe4z"));
    EXPECT_FALSE(latin1.unencodable.has_value());
    EXPECT_EQ(utf8_length("a\xc3\xa4\xe2\x98\x83"), 3u);
}

// ================================================================
// Errors
// ================================================================

TEST(RwsErrors, MapToGatewayResponses) {
    EXPECT_EQ(to_http_error(RwsError(std::nullopt, "Robot controller unreachable at x")).status, 503);
    EXPECT_EQ(to_http_error(RwsError(std::nullopt, "Robot controller unreachable at x")).detail, "Robot controller unreachable at x");
    EXPECT_EQ(to_http_error(RwsError(400, "bad")).status, 400);
    EXPECT_EQ(to_http_error(RwsError(400, "bad")).detail, "Robot controller: bad");
    EXPECT_EQ(to_http_error(RwsError(404, "x")).status, 404);
    EXPECT_EQ(to_http_error(RwsError(403, "x")).status, 409);
    EXPECT_EQ(to_http_error(RwsError(409, "x")).status, 409);
    EXPECT_EQ(to_http_error(RwsError(500, "x")).status, 502);
    EXPECT_EQ(to_http_error(RwsError(502, "x")).status, 502);
}

}  // namespace
}  // namespace gw::rws
