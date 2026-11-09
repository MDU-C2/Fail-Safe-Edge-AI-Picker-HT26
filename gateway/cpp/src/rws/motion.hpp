#pragma once

#include <array>
#include <atomic>
#include <map>
#include <optional>
#include <set>
#include <stdexcept>
#include <string>

#include "config/settings.hpp"
#include "rws/controller.hpp"

namespace gw::rws {

// ================================================================
// RWS motion
// ================================================================
//
// gateway/rws/motion.py: commands the arm tasks of the RAPID program
// (rapid/) through the PERS variables of rapid/SharedVars.mod. The
// job is written first (target, go_home or grip_cmd; for moves and
// home the speed: move_speed, move_acc, move_ramp; for the gripper
// grip_force or grip_pos), then move_request_<arm> is set to TRUE.
// The arm task does the job, writes move_ok_<arm> and clears
// move_request_<arm>, which is polled here.
//
// The arm tasks check no safe zone, so targets outside the box are
// refused before anything is written. One job per arm at a time;
// the arms are independent. With wait=false a job answers STARTED
// once written, like the team's CommServer, and status() reports
// BUSY / DONE / ERR move failed. Blocking.
//
// Changes to these variables or to the arm loop in rapid/ must be
// matched here (and in the Python gateway).
//

//
// The legacy CommandResponse: accepted + CommServer-style message.
//
struct CommandResult {
    bool accepted = false;
    std::string message;
};

//
// Speed of a move or home job: TCP speed in mm/s (v_tcp of the
// RAPID speeddata), and RAPID AccSet's acceleration and ramp in %.
//
struct MotionSpeed {
    double speed = 50.0;
    double acceleration = 100.0;
    double ramp = 100.0;
};

//
// The motion task cannot take a command right now (409).
//
class MotionConflict : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

//
// The arm did not finish in time (504).
//
class MotionTimeout : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

using Vector3 = std::array<double, 3>;
using Quaternion = std::array<double, 4>;

enum class Arm {
    left,
    right,
};

enum class GripperAction {
    grip,
    open,
    move,
};

const char* arm_name(
    Arm arm
);

//
// RAPID motion task of the arm: T_ROB_L / T_ROB_R.
//
const char* arm_task(
    Arm arm
);

//
// Quaternion [q1, q2, q3, q4] of RAPID's OrientZYX(rz, ry, rx), in
// degrees: rotation about z, then the new y, then the new x.
//
Quaternion orient_zyx(
    double rz,
    double ry,
    double rx
);

//
// "[1.000000,2.000000]", as motion.py's rapid_array.
//
std::string rapid_array(
    const double* values,
    std::size_t count
);

class RwsMotion {
public:
    static constexpr double DEFAULT_GRIP_FORCE = 20.0;

    RwsMotion(
        Controller& controller,
        double timeout_seconds,
        Vector3 safe_zone_min,
        Vector3 safe_zone_max,
        MotionSpeed default_speed = {},
        double poll_interval_seconds = 0.05
    );

    //
    // Move tool0 to position (mm, wobj0). rotation is (rx, ry, rz)
    // in degrees, or nothing to keep the current orientation.
    //
    CommandResult move(
        Arm arm,
        const Vector3& position,
        const std::optional<Vector3>& rotation,
        bool wait = true,
        const std::optional<MotionSpeed>& speed = std::nullopt
    );

    CommandResult home(
        Arm arm,
        bool wait = true,
        const std::optional<MotionSpeed>& speed = std::nullopt
    );

    //
    // position: the opening in mm, for move (and only move); force
    // in N, only for grip (DEFAULT_GRIP_FORCE without). A wrong
    // combination is std::invalid_argument.
    //
    CommandResult gripper(
        Arm arm,
        GripperAction action,
        bool wait = true,
        const std::optional<double>& position = std::nullopt,
        const std::optional<double>& force = std::nullopt
    );

    //
    // The arm's last job, as CommServer answers STATUS: BUSY while
    // it runs, then DONE or ERR move failed. Also jobs of others.
    //
    CommandResult status(
        Arm arm
    );

    bool in_safe_zone(
        const Vector3& position
    ) const;

    //
    // A job sent through the gateway has not finished yet.
    //
    bool is_moving() const;

    const MotionSpeed& default_speed() const {
        return default_speed_;
    }

private:
    using Values = std::map<std::string, std::string>;

    CommandResult run(
        Arm arm,
        Values values,
        bool wait,
        const std::set<std::string>& optional = {}
    );

    CommandResult result(
        Arm arm
    );

    Values speed_values(
        const std::optional<MotionSpeed>& speed
    ) const;

    void check_loaded(
        Arm arm
    );

    void check_ready(
        Arm arm
    );

    bool has_variable(
        Arm arm,
        const std::string& name
    );

    void wait_for_job(
        Arm arm
    );

    std::string read(
        Arm arm,
        const std::string& name
    );

    void write(
        Arm arm,
        const std::string& name,
        const std::string& value
    );

    Controller& controller_;

    double timeout_seconds_;
    double poll_interval_seconds_;

    Vector3 safe_zone_min_;
    Vector3 safe_zone_max_;

    MotionSpeed default_speed_;

    std::array<std::atomic<bool>, 2> busy_{};
};

}  // namespace gw::rws
