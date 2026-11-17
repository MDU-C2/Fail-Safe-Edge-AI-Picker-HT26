// Event bus: dotted-prefix subscriptions, direct sends, and dropping
// connections whose send fails.

#include <gtest/gtest.h>

#include <vector>

#include "events/bus.hpp"

namespace gw {
namespace {

Identity identity(
    const std::string& client_id
) {
    return Identity{client_id, false, {}, 0};
}

TEST(Events, PrefixMatching) {
    EXPECT_TRUE(matches_subscription("control.acquired", "control"));
    EXPECT_TRUE(matches_subscription("control.acquired", "control.acquired"));
    EXPECT_FALSE(matches_subscription("controller.x", "control"));
    EXPECT_FALSE(matches_subscription("control", "control.acquired"));
    EXPECT_TRUE(matches_subscription("robot.rapid.loaded", "robot.rapid"));
}

TEST(Events, PublishReachesMatchingSubscribersOnly) {
    EventBus bus;

    std::vector<std::string> a_received;
    std::vector<std::string> b_received;

    auto a = bus.connect(identity("a"), [&](const std::string& text) { a_received.push_back(text); return true; });
    auto b = bus.connect(identity("b"), [&](const std::string& text) { b_received.push_back(text); return true; });

    a->subscribe({"control"});
    b->subscribe({"camera"});

    bus.publish("control.renewed", http::Json{{"client_id", "x"}});

    ASSERT_EQ(a_received.size(), 1u);
    EXPECT_EQ(a_received[0], R"({"type":"control.renewed","data":{"client_id":"x"}})");
    EXPECT_TRUE(b_received.empty());

    EXPECT_TRUE(a->unsubscribe({"control"}).empty());

    bus.publish("control.renewed", http::Json::object());

    EXPECT_EQ(a_received.size(), 1u);
}

TEST(Events, SendToClientIgnoresSubscriptions) {
    EventBus bus;

    int received = 0;

    bus.connect(identity("primary"), [&](const std::string&) { ++received; return true; });
    bus.connect(identity("other"), [&](const std::string&) { ADD_FAILURE(); return true; });

    bus.send_to_client("primary", http::Json{{"type", "control.revoked"}});

    EXPECT_EQ(received, 1);
}

TEST(Events, FailedSendDisconnects) {
    EventBus bus;

    auto connection = bus.connect(identity("a"), [](const std::string&) { return false; });

    connection->subscribe({"camera"});

    bus.publish("camera.status", http::Json::object());

    EXPECT_EQ(bus.connection_count(), 0u);
}

}  // namespace
}  // namespace gw
