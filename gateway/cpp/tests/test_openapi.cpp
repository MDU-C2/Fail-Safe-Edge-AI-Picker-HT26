// openapi.json against the routes: gateway/cpp/openapi/openapi.json
// is maintained by hand (the C++ gateway is its source of truth), so
// this checks every HTTP route the gateway registers has its path and
// method there, and the schema lists no route the gateway lacks.

#include <gtest/gtest.h>

#include <filesystem>
#include <set>
#include <string>

#include "app.hpp"

namespace gw::embedded {

extern const std::string_view openapi_json;

}  // namespace gw::embedded

namespace gw {
namespace {

using Endpoints = std::set<std::string>;

//
// Routes the schema leaves out on purpose: the schema itself, as
// FastAPI did.
//
const Endpoints NOT_IN_SCHEMA = {
    "GET /openapi.json",
};

Endpoints routed_endpoints() {
    Settings settings;

    settings.camera_provider = "mock";
    settings.robot_provider = "mock";
    settings.auth_clients = "admin:1000:*:" + std::string(64, '0');
    settings.calibration_file = (std::filesystem::temp_directory_path() / "gateway-test-openapi-calibration.json").string();

    const App app(settings);

    Endpoints endpoints;

    for (const auto& [method, pattern] : app.router().routes()) {
        const std::string endpoint = method + " " + pattern;

        if (!NOT_IN_SCHEMA.contains(endpoint)) {
            endpoints.insert(endpoint);
        }
    }

    return endpoints;
}

Endpoints schema_endpoints() {
    const auto schema = http::Json::parse(embedded::openapi_json);

    Endpoints endpoints;

    for (const auto& [path, item] : schema.at("paths").items()) {
        for (const auto& [method, operation] : item.items()) {
            std::string upper = method;

            for (auto& character : upper) {
                character = static_cast<char>(std::toupper(static_cast<unsigned char>(character)));
            }

            endpoints.insert(upper + " " + path);
        }
    }

    return endpoints;
}

std::string difference(
    const Endpoints& left,
    const Endpoints& right
) {
    std::string text;

    for (const auto& endpoint : left) {
        if (!right.contains(endpoint)) {
            text += "\n  " + endpoint;
        }
    }

    return text;
}

TEST(OpenApi, SchemaMatchesTheRoutes) {
    const auto routed = routed_endpoints();
    const auto documented = schema_endpoints();

    EXPECT_EQ(difference(routed, documented), "")
        << "routes missing from gateway/cpp/openapi/openapi.json";

    EXPECT_EQ(difference(documented, routed), "")
        << "openapi.json paths the gateway does not route";
}

}  // namespace
}  // namespace gw
