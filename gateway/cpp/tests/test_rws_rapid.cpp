// The RAPID program helpers behind /robot/rapid: value comparison,
// writing variables for a reset, module lookup and ISO-8859-1.

#include <gtest/gtest.h>

#include <functional>
#include <string>
#include <vector>

#include "fake_arm_tasks.hpp"
#include "rws/rapid.hpp"

namespace gw::rws {
namespace {

using testing::FakeArmTasks;

//
// Writes values back the way the controller does: without spaces.
//
class SpacelessController : public FakeArmTasks {
public:
    void write_symbol(
        const std::string& task,
        const std::string& name,
        const std::string& value
    ) override {
        std::string stored;

        for (const char c : value) {
            if (c != ' ') {
                stored.push_back(c);
            }
        }

        FakeArmTasks::write_symbol(task, name, stored);
    }
};

//
// Keeps one variable at its value whatever is written.
//
class StuckController : public FakeArmTasks {
public:
    void write_symbol(
        const std::string& task,
        const std::string& name,
        const std::string& value
    ) override {
        FakeArmTasks::write_symbol(task, name, name == "move_acc_left" ? "50" : value);
    }
};

class ModulesController : public FakeArmTasks {
public:
    nlohmann::json tasks() override {
        return nlohmann::json::array({
            {{"name", "T_ROB_L"}, {"type", "norm"}},
            {{"name", "T_ROB_R"}, {"type", "norm"}},
        });
    }

    std::vector<std::string> program_modules(
        const std::string& task
    ) override {
        if (task == "T_ROB_L") {
            return {"SharedVars", "LeftArm"};
        }

        return {};
    }
};

int status_of(
    const std::function<void()>& call
) {
    try {
        call();

    } catch (const http::HttpError& error) {
        return error.status;
    }

    return 0;
}

TEST(SameRapidValue, IgnoresSpacesAndCaseExceptInStrings) {
    EXPECT_TRUE(same_rapid_value("[0, 0, 0]", "[0,0,0]"));
    EXPECT_TRUE(same_rapid_value("true", "TRUE"));
    EXPECT_TRUE(same_rapid_value("[50,500,5000,1000]", "[50, 500, 5000, 1000]"));

    EXPECT_FALSE(same_rapid_value("[0,0,1]", "[0,0,0]"));
    EXPECT_FALSE(same_rapid_value("TRUE", "FALSE"));

    EXPECT_TRUE(same_rapid_value("\"a b\"", "\"a b\""));
    EXPECT_FALSE(same_rapid_value("\"a b\"", "\"ab\""));
    EXPECT_FALSE(same_rapid_value("\"grip\"", "\"GRIP\""));
}

TEST(WriteVariables, WritesEachThenChecksTheValuesReadBack) {
    SpacelessController controller;

    write_variables(
        controller,
        "T_ROB_L",
        {
            {"move_request_left", "FALSE"},
            {"target_pos_left", "[1, 2, 3]"},
            {"grip_cmd_left", "\"\""},
        }
    );

    EXPECT_EQ(controller.symbol("target_pos_left"), "[1,2,3]");

    const auto writes = controller.written();

    ASSERT_EQ(writes.size(), 3u);

    for (const auto& [task, name, value] : writes) {
        EXPECT_EQ(task, "T_ROB_L");
    }
}

TEST(WriteVariables, AValueThatDoesNotHoldIs502) {
    StuckController controller;

    EXPECT_EQ(
        status_of([&]() { write_variables(controller, "T_ROB_L", {{"move_acc_left", "100"}}); }),
        502
    );
}

TEST(RequireLoadedModule, FindsTheControllersSpellingOr404) {
    ModulesController controller;

    EXPECT_EQ(require_loaded_module(controller, "T_ROB_L", "leftarm"), "LeftArm");

    EXPECT_EQ(status_of([&]() { require_loaded_module(controller, "T_ROB_L", "Other"); }), 404);
    EXPECT_EQ(status_of([&]() { require_loaded_module(controller, "T_ROB_R", "LeftArm"); }), 404);
}

TEST(RequireTask, UnknownTaskIs404ButARunningProgramIsFine) {
    ModulesController controller;

    controller.set_state("running");

    EXPECT_EQ(status_of([&]() { require_task(controller, "T_ROB_L"); }), 0);
    EXPECT_EQ(status_of([&]() { require_task(controller, "T_NOPE"); }), 404);

    EXPECT_EQ(status_of([&]() { require_stopped_task(controller, "T_ROB_L"); }), 409);
}

TEST(Latin1ToUtf8, RoundTripsWithToLatin1) {
    const std::string latin1 = "! Sm\xe5 \xf6" "ar \xb0\n";

    const std::string utf8 = latin1_to_utf8(latin1);

    EXPECT_EQ(utf8, "! Sm\xc3\xa5 \xc3\xb6" "ar \xc2\xb0\n");
    EXPECT_EQ(to_latin1(utf8).bytes, latin1);
    EXPECT_EQ(latin1_to_utf8("MODULE A\n"), "MODULE A\n");
}

}  // namespace
}  // namespace gw::rws
