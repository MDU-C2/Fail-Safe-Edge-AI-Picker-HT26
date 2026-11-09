// ClientRegistry: auth_clients.json is the one list of clients for
// both security modes. A port of gateway/tests/test_client_registry.py.

#include <gtest/gtest.h>

#include <unistd.h>

#include <cstdlib>
#include <filesystem>
#include <fstream>

#include <nlohmann/json.hpp>

#include "security/client_registry.hpp"
#include "util/sha256.hpp"

namespace gw {
namespace {

namespace fs = std::filesystem;

using nlohmann::ordered_json;

class ClientRegistryTest : public ::testing::Test {
protected:
    void SetUp() override {
        directory = fs::temp_directory_path() / ("gateway-registry-" + std::to_string(::getpid()));

        fs::create_directories(directory);
    }

    void TearDown() override {
        fs::remove_all(directory);
    }

    std::string write(
        const std::string& text
    ) {
        const fs::path path = directory / "clients.json";

        std::ofstream(path) << text;

        return path.string();
    }

    static ordered_json entry(
        const std::string& token,
        const std::vector<std::string>& permissions,
        long long priority
    ) {
        return ordered_json{
            {"token_hash", sha256_hex(token)},
            {"permissions", permissions},
            {"control_priority", priority},
        };
    }

    fs::path directory;
};

TEST_F(ClientRegistryTest, IdentitiesComeFromTheFile) {
    const ordered_json clients = {
        {"low", entry("low-token", {"robot.read"}, 5)},
        {"custom", entry("custom-token", {"robot.read", "robot.control"}, 42)},
    };

    const ClientRegistry registry(write(clients.dump()));

    const auto custom = registry.identity("custom", false);

    ASSERT_TRUE(custom.has_value());
    EXPECT_EQ(custom->permissions, (std::set<std::string>{"robot.read", "robot.control"}));
    EXPECT_EQ(custom->control_priority, 42);
    EXPECT_FALSE(custom->authenticated);

    EXPECT_FALSE(registry.identity("admin", false).has_value());
    EXPECT_EQ(registry.default_client_id(), "custom");

    const auto authenticated = registry.authenticate("low-token");

    ASSERT_TRUE(authenticated.has_value());
    EXPECT_EQ(authenticated->client_id, "low");
    EXPECT_TRUE(authenticated->authenticated);

    EXPECT_FALSE(registry.authenticate("wrong").has_value());
}

class InvalidClientsFile : public ClientRegistryTest, public ::testing::WithParamInterface<const char*> {};

TEST_P(InvalidClientsFile, IsRejected) {
    EXPECT_THROW(ClientRegistry(write(GetParam())), RegistryError);
}

INSTANTIATE_TEST_SUITE_P(
    ClientRegistry,
    InvalidClientsFile,
    ::testing::Values(
        "[]",
        "{}",
        R"({"x": {"permissions": [], "control_priority": 1}})",
        R"({"x": {"token_hash": "h", "permissions": "robot.read", "control_priority": 1}})",
        R"({"x": {"token_hash": "h", "permissions": [1], "control_priority": 1}})",
        R"({"x": {"token_hash": "h", "permissions": [], "control_priority": "high"}})",
        "not json",
        R"({"x": 1})"
    )
);

TEST_F(ClientRegistryTest, MissingFileIsRejected) {
    EXPECT_THROW(ClientRegistry((directory / "missing.json").string()), RegistryError);
}

TEST_F(ClientRegistryTest, WildcardGrantsEverything) {
    const ClientRegistry registry(write(ordered_json{{"admin", entry("t", {"*"}, 1)}}.dump()));

    EXPECT_TRUE(registry.identity("admin", false)->can("robot.program"));
}

TEST_F(ClientRegistryTest, DefaultIsTheFirstOfTheHighestPriority) {
    const ordered_json clients = {
        {"b", entry("1", {}, 7)},
        {"a", entry("2", {}, 7)},
        {"c", entry("3", {}, 1)},
    };

    EXPECT_EQ(ClientRegistry(write(clients.dump())).default_client_id(), "b");
}

TEST(RepositoryClientsFile, IsValid) {
    //
    // The tests run from the repository root; the Docker build
    // copies only gateway/cpp, so the file may be missing there.
    //
    if (!fs::exists("gateway/auth_clients.json")) {
        GTEST_SKIP() << "gateway/auth_clients.json not here";
    }

    const ClientRegistry registry("gateway/auth_clients.json");

    EXPECT_EQ(registry.default_client_id(), "admin");

    for (const char* client_id : {"admin", "primary", "secondary", "tertiary", "diagnostics"}) {
        EXPECT_TRUE(registry.identity(client_id, false).has_value()) << client_id;
    }

    EXPECT_TRUE(registry.identity("admin", false)->can("robot.program"));

    for (const char* client_id : {"primary", "secondary", "tertiary", "diagnostics"}) {
        EXPECT_FALSE(registry.identity(client_id, false)->can("robot.program")) << client_id;
    }
}

}  // namespace
}  // namespace gw
