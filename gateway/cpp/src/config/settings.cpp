#include "config/settings.hpp"

#include <functional>
#include <initializer_list>
#include <optional>
#include <vector>

#include <nlohmann/json.hpp>

#include "config/dotenv.hpp"
#include "util/text.hpp"

extern char** environ;

namespace gw {

namespace {

constexpr const char* PREFIX = "GATEWAY_";
constexpr const char* DOTENV_FILE = "gateway/.env";

//
// Upper-cased names, so lookups ignore case like pydantic-settings.
//
std::map<std::string, std::string> upper_keys(
    const std::map<std::string, std::string>& values
) {
    std::map<std::string, std::string> result;

    for (const auto& [key, value] : values) {
        result[to_upper(key)] = value;
    }

    return result;
}

class Loader {
public:
    Loader(
        const std::map<std::string, std::string>& environment,
        const std::map<std::string, std::string>& dotenv
    )
        : environment_(upper_keys(environment)),
          dotenv_(upper_keys(dotenv)) {}

    void string(
        const char* name,
        std::string& target
    ) {
        if (const auto value = lookup(name)) {
            target = *value;
        }
    }

    void choice(
        const char* name,
        std::string& target,
        std::initializer_list<const char*> choices
    ) {
        const auto value = lookup(name);

        if (!value) {
            return;
        }

        std::string allowed;

        for (const char* option : choices) {
            if (*value == option) {
                target = *value;
                return;
            }

            allowed += allowed.empty() ? "" : ", ";
            allowed += format("'%s'", option);
        }

        fail(name, *value, "must be one of " + allowed);
    }

    void number(
        const char* name,
        double& target
    ) {
        const auto value = lookup(name);

        if (!value) {
            return;
        }

        const auto parsed = parse_double(*value);

        if (!parsed) {
            fail(name, *value, "must be a number");
        }

        target = *parsed;
    }

    void integer(
        const char* name,
        long long& target
    ) {
        const auto value = lookup(name);

        if (!value) {
            return;
        }

        const auto parsed = parse_integer(*value);

        if (!parsed) {
            fail(name, *value, "must be an integer");
        }

        target = *parsed;
    }

    //
    // The strings pydantic accepts for a bool, in any case.
    //
    void boolean(
        const char* name,
        bool& target
    ) {
        const auto value = lookup(name);

        if (!value) {
            return;
        }

        const std::string text = to_lower(std::string(trim(*value)));

        for (const char* yes : {"1", "true", "t", "yes", "y", "on"}) {
            if (text == yes) {
                target = true;
                return;
            }
        }

        for (const char* no : {"0", "false", "f", "no", "n", "off"}) {
            if (text == no) {
                target = false;
                return;
            }
        }

        fail(name, *value, "must be true or false");
    }

    //
    // tuple[float, float, float]: pydantic-settings parses complex
    // types as JSON, e.g. [180,-400,50].
    //
    void vector3(
        const char* name,
        Vector3& target
    ) {
        const auto value = lookup(name);

        if (!value) {
            return;
        }

        const auto parsed = nlohmann::json::parse(*value, nullptr, false);

        if (!parsed.is_array() || parsed.size() != 3) {
            fail(name, *value, "must be a JSON list of three numbers");
        }

        for (std::size_t i = 0; i < 3; ++i) {
            if (!parsed[i].is_number()) {
                fail(name, *value, "must be a JSON list of three numbers");
            }

            target[i] = parsed[i].get<double>();
        }
    }

private:
    std::optional<std::string> lookup(
        const char* name
    ) const {
        const std::string key = to_upper(std::string(PREFIX) + name);

        if (const auto it = environment_.find(key); it != environment_.end()) {
            return it->second;
        }

        if (const auto it = dotenv_.find(key); it != dotenv_.end()) {
            return it->second;
        }

        return std::nullopt;
    }

    [[noreturn]] static void fail(
        const char* name,
        const std::string& value,
        const std::string& reason
    ) {
        throw SettingsError(
            format(
                "Invalid setting %s%s=%s: %s",
                PREFIX,
                to_upper(name).c_str(),
                value.c_str(),
                reason.c_str()
            )
        );
    }

    std::map<std::string, std::string> environment_;
    std::map<std::string, std::string> dotenv_;
};

}  // namespace

Settings load_settings(
    const std::map<std::string, std::string>& environment,
    const std::map<std::string, std::string>& dotenv
) {
    Settings settings;

    Loader load(environment, dotenv);

    load.string("app_name", settings.app_name);
    load.string("api_prefix", settings.api_prefix);
    load.choice("security_mode", settings.security_mode, {"development", "strict"});
    load.string("auth_clients_file", settings.auth_clients_file);

    load.string("tls_cert_file", settings.tls_cert_file);
    load.string("tls_key_file", settings.tls_key_file);
    load.boolean("tls_allow_plaintext", settings.tls_allow_plaintext);

    if (settings.tls_cert_file.empty() != settings.tls_key_file.empty()) {
        throw SettingsError(
            "Invalid settings: GATEWAY_TLS_CERT_FILE and GATEWAY_TLS_KEY_FILE must be set together"
        );
    }

    load.number("control_lease_seconds", settings.control_lease_seconds);
    load.number("control_contention_seconds", settings.control_contention_seconds);
    load.number("control_startup_grace_seconds", settings.control_startup_grace_seconds);

    load.string("calibration_file", settings.calibration_file);

    load.choice("camera_provider", settings.camera_provider, {"mock", "oak"});
    load.string("camera_ip", settings.camera_ip);
    load.number("camera_fps", settings.camera_fps);
    load.integer("camera_rgb_width", settings.camera_rgb_width);
    load.integer("camera_rgb_height", settings.camera_rgb_height);
    load.number("camera_max_frame_age_seconds", settings.camera_max_frame_age_seconds);

    load.choice("robot_provider", settings.robot_provider, {"mock", "tcp"});
    load.string("robot_host", settings.robot_host);
    load.integer("robot_port", settings.robot_port);

    load.integer("rws_port", settings.rws_port);
    load.string("rws_user", settings.rws_user);
    load.string("rws_password", settings.rws_password);
    load.number("rws_timeout", settings.rws_timeout);
    load.number("rws_move_timeout", settings.rws_move_timeout);
    load.vector3("rws_safe_zone_min", settings.rws_safe_zone_min);
    load.vector3("rws_safe_zone_max", settings.rws_safe_zone_max);
    load.number("rws_default_speed", settings.rws_default_speed);
    load.number("rws_default_acceleration", settings.rws_default_acceleration);
    load.number("rws_default_ramp", settings.rws_default_ramp);

    return settings;
}

Settings load_settings() {
    std::map<std::string, std::string> environment;

    for (char** entry = environ; entry != nullptr && *entry != nullptr; ++entry) {
        const std::string_view text(*entry);
        const auto equals = text.find('=');

        if (equals != std::string_view::npos && iequals(text.substr(0, 8), PREFIX)) {
            environment[std::string(text.substr(0, equals))] = std::string(text.substr(equals + 1));
        }
    }

    return load_settings(
        environment,
        read_dotenv(DOTENV_FILE)
    );
}

}  // namespace gw
