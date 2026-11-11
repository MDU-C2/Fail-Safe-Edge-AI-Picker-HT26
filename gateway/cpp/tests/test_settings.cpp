// Settings: GATEWAY_* variables over gateway/.env over the defaults,
// with the names, defaults and validation of gateway/config.py.

#include <gtest/gtest.h>

#include "config/dotenv.hpp"
#include "config/settings.hpp"

namespace gw {
namespace {

TEST(Settings, DefaultsMatchThePythonGateway) {
    const Settings settings = load_settings({}, {});

    EXPECT_EQ(settings.security_mode, "strict");
    EXPECT_EQ(settings.api_prefix, "/api/v1");
    EXPECT_EQ(settings.auth_clients_file, "gateway/auth_clients.json");
    EXPECT_EQ(settings.camera_provider, "oak");
    EXPECT_EQ(settings.robot_provider, "tcp");
    EXPECT_EQ(settings.robot_host, "192.168.125.1");
    EXPECT_EQ(settings.robot_port, 5001);
    EXPECT_EQ(settings.rws_port, 80);
    EXPECT_EQ(settings.rws_user, "Default User");
    EXPECT_DOUBLE_EQ(settings.control_lease_seconds, 5.0);
    EXPECT_DOUBLE_EQ(settings.control_contention_seconds, 3.0);
    EXPECT_DOUBLE_EQ(settings.control_startup_grace_seconds, 5.0);
    EXPECT_EQ(settings.camera_rgb_width, 1920);
    EXPECT_EQ(settings.rws_safe_zone_min, (Vector3{180.0, -400.0, 50.0}));
    EXPECT_EQ(settings.rws_safe_zone_max, (Vector3{800.0, 500.0, 600.0}));
}

TEST(Settings, EnvironmentOverridesDotenv) {
    const Settings settings = load_settings(
        {{"GATEWAY_SECURITY_MODE", "development"}},
        {{"GATEWAY_SECURITY_MODE", "strict"}, {"GATEWAY_ROBOT_PORT", "5000"}}
    );

    EXPECT_EQ(settings.security_mode, "development");
    EXPECT_EQ(settings.robot_port, 5000);
}

TEST(Settings, NamesIgnoreCase) {
    const Settings settings = load_settings({{"gateway_camera_provider", "mock"}}, {});

    EXPECT_EQ(settings.camera_provider, "mock");
}

TEST(Settings, ParsesNumbersAndTuples) {
    const Settings settings = load_settings(
        {
            {"GATEWAY_CONTROL_LEASE_SECONDS", "2.0"},
            {"GATEWAY_CAMERA_FPS", "4"},
            {"GATEWAY_RWS_SAFE_ZONE_MIN", "[180,-400,50]"},
            {"GATEWAY_RWS_SAFE_ZONE_MAX", "[800.5, 500, 600]"},
        },
        {}
    );

    EXPECT_DOUBLE_EQ(settings.control_lease_seconds, 2.0);
    EXPECT_DOUBLE_EQ(settings.camera_fps, 4.0);
    EXPECT_EQ(settings.rws_safe_zone_min, (Vector3{180.0, -400.0, 50.0}));
    EXPECT_EQ(settings.rws_safe_zone_max, (Vector3{800.5, 500.0, 600.0}));
}

TEST(Settings, UnknownNamesAreIgnored) {
    EXPECT_NO_THROW(load_settings({{"GATEWAY_NOT_A_SETTING", "x"}}, {}));
}

class InvalidSetting : public ::testing::TestWithParam<std::pair<const char*, const char*>> {};

TEST_P(InvalidSetting, StopsStartup) {
    const auto [name, value] = GetParam();

    EXPECT_THROW(load_settings({{name, value}}, {}), SettingsError);
}

INSTANTIATE_TEST_SUITE_P(
    Settings,
    InvalidSetting,
    ::testing::Values(
        std::pair{"GATEWAY_SECURITY_MODE", "open"},
        std::pair{"GATEWAY_CAMERA_PROVIDER", "usb"},
        std::pair{"GATEWAY_ROBOT_PROVIDER", ""},
        std::pair{"GATEWAY_ROBOT_PORT", "50.5"},
        std::pair{"GATEWAY_ROBOT_PORT", "port"},
        std::pair{"GATEWAY_CONTROL_LEASE_SECONDS", "5s"},
        std::pair{"GATEWAY_RWS_SAFE_ZONE_MIN", "[1,2]"},
        std::pair{"GATEWAY_RWS_SAFE_ZONE_MIN", "180,-400,50"},
        std::pair{"GATEWAY_RWS_SAFE_ZONE_MAX", "[1,2,\"x\"]"}
    )
);

TEST(Dotenv, ParsesTheExampleFileSyntax) {
    const auto values = parse_dotenv(
        "# comment\n"
        "GATEWAY_SECURITY_MODE=development\n"
        "\n"
        "export GATEWAY_RWS_USER=Default User\n"
        "GATEWAY_RWS_PASSWORD=\"rob # otics\"\n"
        "GATEWAY_CAMERA_IP='169.254.1.223'\n"
        "GATEWAY_ROBOT_HOST=127.0.0.1 # the PC\n"
        "  GATEWAY_RWS_SAFE_ZONE_MIN = [180,-400,50]  \r\n"
        "not a line\n"
    );

    EXPECT_EQ(values.at("GATEWAY_SECURITY_MODE"), "development");
    EXPECT_EQ(values.at("GATEWAY_RWS_USER"), "Default User");
    EXPECT_EQ(values.at("GATEWAY_RWS_PASSWORD"), "rob # otics");
    EXPECT_EQ(values.at("GATEWAY_CAMERA_IP"), "169.254.1.223");
    EXPECT_EQ(values.at("GATEWAY_ROBOT_HOST"), "127.0.0.1");
    EXPECT_EQ(values.at("GATEWAY_RWS_SAFE_ZONE_MIN"), "[180,-400,50]");
    EXPECT_EQ(values.count("not a line"), 0u);
}

TEST(Dotenv, MissingFileIsEmpty) {
    EXPECT_TRUE(read_dotenv("/nonexistent/gateway/.env").empty());
}

}  // namespace
}  // namespace gw
