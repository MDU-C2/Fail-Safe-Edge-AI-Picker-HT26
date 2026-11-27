#pragma once

#include <array>
#include <map>
#include <stdexcept>
#include <string>

namespace gw {

// ================================================================
// Settings
// ================================================================
//
// The same settings, names and defaults as gateway/config.py:
// GATEWAY_<NAME> environment variables override gateway/.env, which
// overrides the defaults below. Names are case-insensitive, as in
// pydantic-settings, and unknown names are ignored.
//

using Vector3 = std::array<double, 3>;

struct Settings {
    std::string app_name = "Robot & Camera Gateway";
    std::string api_prefix = "/api/v1";

    //
    // "development" or "strict"
    //
    std::string security_mode = "strict";

    std::string auth_clients_file = "gateway/auth_clients.json";

    //
    // C++ only. The clients on one line, replacing the file when not
    // empty (format in security/client_registry.hpp); the Lynx app
    // environment sets it, since the image has no clients file.
    //
    std::string auth_clients;

    double control_lease_seconds = 5.0;
    double control_contention_seconds = 3.0;
    double control_startup_grace_seconds = 5.0;

    std::string calibration_file = "gateway/calibration.json";

    //
    // "mock" or "oak"
    //
    std::string camera_provider = "oak";

    std::string camera_ip = "169.254.1.223";

    double camera_fps = 4.0;

    long long camera_rgb_width = 1920;
    long long camera_rgb_height = 1080;

    //
    // Undistort CAM_A on the camera, so /camera/rgb and the aligned
    // /camera/depth are pinhole images: /camera/intrinsics is then
    // all a client needs and /camera/distortion gives zeros.
    //
    bool camera_undistort = true;

    double camera_max_frame_age_seconds = 5.0;

    //
    // "mock" or "tcp"
    //
    std::string robot_provider = "tcp";

    std::string robot_host = "192.168.125.1";
    long long robot_port = 5001;

    long long rws_port = 80;
    std::string rws_user = "Default User";
    std::string rws_password = "robotics";
    double rws_timeout = 10.0;

    double rws_move_timeout = 60.0;

    Vector3 rws_safe_zone_min = {180.0, -400.0, 50.0};
    Vector3 rws_safe_zone_max = {800.0, 500.0, 600.0};

    double rws_default_speed = 50.0;
    double rws_default_acceleration = 100.0;
    double rws_default_ramp = 100.0;

    //
    // C++ only. Seconds without the gateway's RWS heartbeat after
    // which an idle arm goes home on its own (rws/heartbeat.hpp); a
    // gateway start also sends the arms home. 0 = off: no heartbeat
    // is written. Off by default, since the default robot host is
    // the real YuMi; the Lynx deployment sets 5.
    //
    double rws_heartbeat_timeout = 0.0;
};

class SettingsError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

//
// Settings from the given environment (name -> value) and .env
// values. Throws SettingsError naming the setting on a bad value.
//
Settings load_settings(
    const std::map<std::string, std::string>& environment,
    const std::map<std::string, std::string>& dotenv
);

//
// Settings from this process's environment and gateway/.env
// (relative to the working directory, like the Python gateway).
//
Settings load_settings();

}  // namespace gw
