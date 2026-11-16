#include "rws/motion.hpp"

#include <chrono>
#include <cmath>
#include <thread>

#include "rws/errors.hpp"
#include "util/text.hpp"

namespace gw::rws {

namespace {

//
// Variables added to rapid/SharedVars.mod later than the rest. An
// older program on the controller lacks them and would refuse the
// write half way through a job, so they are checked first.
//
const std::map<std::string, std::string> NEWER_VARIABLES = {
    {"move_speed", "speed variables"},
    {"grip_pos", "gripper position variables"},
    {"grip_force", "gripper force variables"},
};

//
// The rest of the speeddata, as in RAPID's v5 .. v1000: orientation
// speed in degrees/s, arm angle speed in mm/s and degrees/s.
//
constexpr double SPEEDDATA_REST[] = {500.0, 5000.0, 1000.0};

std::string fixed6(
    double value
) {
    return format("%.6f", value);
}

std::size_t index(
    Arm arm
) {
    return arm == Arm::left ? 0 : 1;
}

}  // namespace

const char* arm_name(
    Arm arm
) {
    return arm == Arm::left ? "left" : "right";
}

const char* arm_task(
    Arm arm
) {
    return arm == Arm::left ? "T_ROB_L" : "T_ROB_R";
}

Quaternion orient_zyx(
    double rz,
    double ry,
    double rx
) {
    auto axis_rotation = [](int axis, double degrees) {
        const double half = degrees * M_PI / 180.0 / 2.0;

        Quaternion q = {std::cos(half), 0.0, 0.0, 0.0};

        q[1 + axis] = std::sin(half);

        return q;
    };

    auto multiply = [](const Quaternion& a, const Quaternion& b) {
        const auto [w1, x1, y1, z1] = a;
        const auto [w2, x2, y2, z2] = b;

        return Quaternion{
            w1 * w2 - x1 * x2 - y1 * y2 - z1 * z2,
            w1 * x2 + x1 * w2 + y1 * z2 - z1 * y2,
            w1 * y2 - x1 * z2 + y1 * w2 + z1 * x2,
            w1 * z2 + x1 * y2 - y1 * x2 + z1 * w2,
        };
    };

    return multiply(
        multiply(
            axis_rotation(2, rz),
            axis_rotation(1, ry)
        ),
        axis_rotation(0, rx)
    );
}

std::string rapid_array(
    const double* values,
    std::size_t count
) {
    std::string text = "[";

    for (std::size_t i = 0; i < count; ++i) {
        text += (i == 0 ? "" : ",") + fixed6(values[i]);
    }

    return text + "]";
}

RwsMotion::RwsMotion(
    Controller& controller,
    double timeout_seconds,
    Vector3 safe_zone_min,
    Vector3 safe_zone_max,
    MotionSpeed default_speed,
    double poll_interval_seconds
)
    : controller_(controller),
      timeout_seconds_(timeout_seconds),
      poll_interval_seconds_(poll_interval_seconds),
      safe_zone_min_(safe_zone_min),
      safe_zone_max_(safe_zone_max),
      default_speed_(default_speed) {}

// ================================================================
// Jobs
// ================================================================

CommandResult RwsMotion::move(
    Arm arm,
    const Vector3& position,
    const std::optional<Vector3>& rotation,
    bool wait,
    const std::optional<MotionSpeed>& speed
) {
    if (!in_safe_zone(position)) {
        return CommandResult{false, "ERR outside safe zone"};
    }

    Values values = speed_values(speed);

    values["target_pos"] = rapid_array(position.data(), 3);
    values["use_rot"] = rotation ? "TRUE" : "FALSE";
    values["go_home"] = "FALSE";
    values["grip_cmd"] = "\"\"";

    if (rotation) {
        const auto [rx, ry, rz] = *rotation;

        values["target_rot"] = rapid_array(orient_zyx(rz, ry, rx).data(), 4);
    }

    return run(arm, std::move(values), wait);
}

CommandResult RwsMotion::home(
    Arm arm,
    bool wait,
    const std::optional<MotionSpeed>& speed
) {
    Values values = speed_values(speed);

    values["go_home"] = "TRUE";
    values["grip_cmd"] = "\"\"";

    return run(arm, std::move(values), wait);
}

CommandResult RwsMotion::gripper(
    Arm arm,
    GripperAction action,
    bool wait,
    const std::optional<double>& position,
    const std::optional<double>& force
) {
    if ((action == GripperAction::move) != position.has_value()) {
        throw std::invalid_argument("position is required for move and only for move");
    }

    if (force && action != GripperAction::grip) {
        throw std::invalid_argument("force is only for grip");
    }

    static const char* const COMMANDS[] = {"GRIP", "OPEN", "MOVE"};

    Values values = {
        {"go_home", "FALSE"},
        {"grip_cmd", format("\"%s\"", COMMANDS[static_cast<int>(action)])},
    };

    std::set<std::string> optional;

    if (action == GripperAction::move) {
        values["grip_pos"] = fixed6(*position);
    }

    if (action == GripperAction::grip) {
        values["grip_force"] = fixed6(force.value_or(DEFAULT_GRIP_FORCE));

        //
        // A program from before grip_force grips with the default
        // force anyway.
        //
        if (!force) {
            optional.insert("grip_force");
        }
    }

    return run(arm, std::move(values), wait, optional);
}

CommandResult RwsMotion::status(
    Arm arm
) {
    check_loaded(arm);

    if (read(arm, "move_request") == "TRUE") {
        return CommandResult{true, "BUSY"};
    }

    return result(arm);
}

bool RwsMotion::in_safe_zone(
    const Vector3& position
) const {
    for (std::size_t i = 0; i < 3; ++i) {
        if (!(safe_zone_min_[i] <= position[i] && position[i] <= safe_zone_max_[i])) {
            return false;
        }
    }

    return true;
}

bool RwsMotion::is_moving() const {
    return busy_[0].load() || busy_[1].load();
}

RwsMotion::Values RwsMotion::speed_values(
    const std::optional<MotionSpeed>& speed
) const {
    const MotionSpeed& chosen = speed ? *speed : default_speed_;

    const double speeddata[] = {chosen.speed, SPEEDDATA_REST[0], SPEEDDATA_REST[1], SPEEDDATA_REST[2]};

    return Values{
        {"move_speed", rapid_array(speeddata, 4)},
        {"move_acc", fixed6(chosen.acceleration)},
        {"move_ramp", fixed6(chosen.ramp)},
    };
}

// ================================================================
// Handshake
// ================================================================

CommandResult RwsMotion::run(
    Arm arm,
    Values values,
    bool wait,
    const std::set<std::string>& optional
) {
    std::atomic<bool>& busy = busy_[index(arm)];

    bool expected = false;

    if (!busy.compare_exchange_strong(expected, true)) {
        throw MotionConflict(format("A %s arm job is already in progress", arm_name(arm)));
    }

    struct Release {
        std::atomic<bool>& flag;

        ~Release() {
            flag = false;
        }
    } release{busy};

    check_ready(arm);

    //
    // Of the newer variables, those in optional are left out when
    // the program lacks them; any other missing one refuses the job
    // before anything is written.
    //
    for (const auto& [name, description] : NEWER_VARIABLES) {
        if (values.count(name) == 0) {
            continue;
        }

        if (optional.count(name) > 0) {
            if (!has_variable(arm, name)) {
                values.erase(name);
            }

            continue;
        }

        if (!has_variable(arm, name)) {
            throw MotionConflict(
                format(
                    "The RAPID program in %s has no %s (no %s_%s); upload rapid/ again (rapidctl.py upload)",
                    arm_task(arm),
                    description.c_str(),
                    name.c_str(),
                    arm_name(arm)
                )
            );
        }
    }

    //
    // In the Python gateway's order (dict insertion order): the
    // job's variables as motion.py builds them.
    //
    static const char* const ORDER[] = {
        "target_pos",
        "use_rot",
        "go_home",
        "grip_cmd",
        "move_speed",
        "move_acc",
        "move_ramp",
        "target_rot",
        "grip_pos",
        "grip_force",
    };

    for (const char* name : ORDER) {
        if (const auto it = values.find(name); it != values.end()) {
            write(arm, it->first, it->second);
        }
    }

    write(arm, "move_request", "TRUE");

    //
    // The arm task now owns the job; a new one is refused until it
    // clears move_request (see check_ready).
    //
    if (!wait) {
        return CommandResult{true, "STARTED"};
    }

    wait_for_job(arm);

    return result(arm);
}

CommandResult RwsMotion::result(
    Arm arm
) {
    if (read(arm, "move_ok") == "TRUE") {
        return CommandResult{true, "DONE"};
    }

    //
    // Same reply as the team's CommServer: the arm tasks only
    // report move_ok (an unreachable target or a home move that
    // stopped early).
    //
    return CommandResult{false, "ERR move failed"};
}

void RwsMotion::check_loaded(
    Arm arm
) {
    if (!has_variable(arm, "move_request")) {
        throw MotionConflict(
            format(
                "The motion program is not loaded in %s (no move_request_%s variable)",
                arm_task(arm),
                arm_name(arm)
            )
        );
    }
}

bool RwsMotion::has_variable(
    Arm arm,
    const std::string& name
) {
    try {
        read(arm, name);

    } catch (const RwsError& error) {
        if (error.status != 400) {
            throw;
        }

        return false;
    }

    return true;
}

void RwsMotion::check_ready(
    Arm arm
) {
    if (controller_.execution_state() != "running") {
        throw MotionConflict("RAPID is not running");
    }

    check_loaded(arm);

    //
    // Still set while an earlier job runs: one started with
    // wait=false, one that timed out here, or one from another
    // client such as the team's CommServer.
    //
    if (read(arm, "move_request") == "TRUE") {
        throw MotionConflict(format("The %s arm is busy with an earlier job", arm_name(arm)));
    }
}

void RwsMotion::wait_for_job(
    Arm arm
) {
    const auto deadline = std::chrono::steady_clock::now()
        + std::chrono::duration_cast<std::chrono::steady_clock::duration>(
            std::chrono::duration<double>(timeout_seconds_)
        );

    while (read(arm, "move_request") == "TRUE") {
        if (controller_.execution_state() != "running") {
            throw MotionConflict("RAPID stopped before the move finished");
        }

        if (std::chrono::steady_clock::now() > deadline) {
            throw MotionTimeout(
                format("The %s arm did not finish within %s s", arm_name(arm), format_g(timeout_seconds_).c_str())
            );
        }

        std::this_thread::sleep_for(std::chrono::duration<double>(poll_interval_seconds_));
    }
}

std::string RwsMotion::read(
    Arm arm,
    const std::string& name
) {
    return controller_.read_symbol(arm_task(arm), name + "_" + arm_name(arm));
}

void RwsMotion::write(
    Arm arm,
    const std::string& name,
    const std::string& value
) {
    controller_.write_symbol(arm_task(arm), name + "_" + arm_name(arm), value);
}

}  // namespace gw::rws
