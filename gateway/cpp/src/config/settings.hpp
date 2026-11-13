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
    // TLS for clients (gateway/cpp/TLS.md): the gateway's PEM
    // certificate and unencrypted key, both or neither. Strict mode
    // refuses to start without them unless tls_allow_plaintext is
    // set. C++ gateway only; the Python gateway takes them as
    // uvicorn's --ssl-certfile / --ssl-keyfile.
    //
    std::string tls_cert_file;
    std::string tls_key_file;
    bool tls_allow_plaintext = false;

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
