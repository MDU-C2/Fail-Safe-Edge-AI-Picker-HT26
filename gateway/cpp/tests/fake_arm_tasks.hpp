#pragma once

// A fake controller with the arm tasks of rapid/SharedVars.mod, for
// the RWS motion and queue tests.

#include <algorithm>
#include <chrono>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <tuple>
#include <utility>
#include <vector>

#include "rws/controller.hpp"
#include "rws/errors.hpp"

namespace gw::rws::testing {

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
                     {"path_len", "0"},
                     {"path_pos", "[[0,0,0],[0,0,0],[0,0,0],[0,0,0],[0,0,0],[0,0,0],[0,0,0],[0,0,0]]"},
                     {"path_acc", "0"},
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
        double x = std::stod(target.substr(1, target.find(',') - 1));

        //
        // A path job: every point must be in reach, as RAPID's
        // RunPath checks them all first.
        //
        const auto length = symbols_.find("path_len_" + arm);

        if (length != symbols_.end() && std::stoi(length->second) > 0 && symbols_["go_home_" + arm] != "TRUE"
            && symbols_["grip_cmd_" + arm] == "\"\"") {
            const std::string points = symbols_["path_pos_" + arm];

            x = 0.0;

            std::size_t at = 0;

            for (int i = 0; i < std::stoi(length->second); ++i) {
                at = points.find("[", at + 1);
                x = std::max(x, std::stod(points.substr(at + 1)));
            }

            length->second = "0";
        }

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

}  // namespace gw::rws::testing
