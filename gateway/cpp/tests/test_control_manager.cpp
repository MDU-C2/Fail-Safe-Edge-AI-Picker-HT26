// ControlManager on its own, with a fake clock: the contention window
// that gives a free lease to the highest client trying, and the
// startup grace. A port of gateway/tests/test_control_manager.py.

#include <gtest/gtest.h>

#include <map>
#include <memory>

#include "control/manager.hpp"

namespace gw {
namespace {

constexpr double LEASE = 5.0;
constexpr double WINDOW = 3.0;

const std::map<std::string, long long> PRIORITY = {
    {"admin", 1000},
    {"primary", 300},
    {"secondary", 200},
    {"tertiary", 100},
};

class FakeClock : public Clock {
public:
    SteadyTime steady() const override {
        return steady_;
    }

    WallTime wall() const override {
        return wall_;
    }

    void advance(
        double seconds
    ) {
        steady_ = add_seconds(steady_, seconds);
        wall_ = add_seconds(wall_, seconds);
    }

private:
    SteadyTime steady_{};
    WallTime wall_{std::chrono::seconds(1'767'225'600)};
};

class ControlManagerTest : public ::testing::Test {
protected:
    std::unique_ptr<ControlManager> make_manager(
        double grace = 0.0
    ) {
        return std::make_unique<ControlManager>(LEASE, WINDOW, grace, clock);
    }

    AcquireResult acquire(
        const std::string& client_id
    ) {
        return manager->acquire(client_id, PRIORITY.at(client_id));
    }

    bool renew(
        const std::string& client_id
    ) {
        return manager->renew(client_id, PRIORITY.at(client_id)).success;
    }

    std::optional<std::string> holder() {
        const auto lease = manager->get();

        return lease ? std::optional(lease->client_id) : std::nullopt;
    }

    FakeClock clock;

    std::unique_ptr<ControlManager> manager = make_manager();
};

// ================================================================
// Contention for a free lease
// ================================================================

TEST_F(ControlManagerTest, FirstClientGetsAFreeLeaseWhenNobodyElseTries) {
    EXPECT_TRUE(acquire("tertiary").success);
    EXPECT_EQ(holder(), "tertiary");
}

TEST_F(ControlManagerTest, LowerTierIsRefusedWhileAHigherOneIsTrying) {
    EXPECT_TRUE(acquire("primary").success);
    EXPECT_FALSE(acquire("secondary").success);

    //
    // Primary dies; its lease lapses. Secondary tried recently, so
    // tertiary asking first is refused and told when to retry.
    //
    clock.advance(LEASE - 1.0);
    EXPECT_FALSE(acquire("secondary").success);

    clock.advance(1.0);
    EXPECT_EQ(holder(), std::nullopt);

    const AcquireResult result = acquire("tertiary");

    EXPECT_FALSE(result.success);
    EXPECT_NE(result.message.find("secondary"), std::string::npos);
    ASSERT_TRUE(result.retry_after.has_value());
    EXPECT_NEAR(*result.retry_after, WINDOW - 1.0, 1e-6);

    EXPECT_TRUE(acquire("secondary").success);
    EXPECT_EQ(holder(), "secondary");
}

TEST_F(ControlManagerTest, ACrashedHolderDoesNotBlockTheNextTier) {
    EXPECT_TRUE(acquire("primary").success);

    //
    // Primary's last renew is a full lease ago when it lapses,
    // longer than the window, so secondary is not held back.
    //
    clock.advance(LEASE);

    EXPECT_TRUE(acquire("secondary").success);
}

TEST_F(ControlManagerTest, LowerTierGetsTheLeaseOnceTheHigherOneStopsTrying) {
    EXPECT_FALSE(renew("secondary"));

    EXPECT_FALSE(acquire("tertiary").success);

    clock.advance(WINDOW);

    EXPECT_TRUE(acquire("tertiary").success);
}

TEST_F(ControlManagerTest, FailedRenewCountsAsContending) {
    EXPECT_TRUE(acquire("secondary").success);

    clock.advance(LEASE);

    //
    // Secondary was partitioned and comes back renewing a lapsed
    // lease: refused, but it is alive, so tertiary waits for it.
    //
    EXPECT_FALSE(renew("secondary"));
    EXPECT_FALSE(acquire("tertiary").success);
    EXPECT_TRUE(acquire("secondary").success);
}

TEST_F(ControlManagerTest, ReleaseWithdrawsFromContention) {
    EXPECT_TRUE(acquire("primary").success);
    EXPECT_TRUE(manager->release("primary").success);

    //
    // Released, so secondary need not wait out primary's window.
    //
    EXPECT_TRUE(acquire("secondary").success);
}

TEST_F(ControlManagerTest, ReleaseWithoutTheLeaseAlsoWithdraws) {
    EXPECT_FALSE(renew("secondary"));
    EXPECT_FALSE(acquire("tertiary").success);

    //
    // Secondary gives up: it holds nothing (409), but no longer
    // counts as contending.
    //
    EXPECT_FALSE(manager->release("secondary").success);

    EXPECT_TRUE(acquire("tertiary").success);
}

TEST_F(ControlManagerTest, EqualOrLowerAttemptsDoNotBlock) {
    EXPECT_FALSE(renew("tertiary"));

    EXPECT_TRUE(acquire("secondary").success);
}

TEST_F(ControlManagerTest, PreemptionIsUnchanged) {
    EXPECT_TRUE(acquire("tertiary").success);
    EXPECT_FALSE(acquire("tertiary").success);

    const AcquireResult result = acquire("primary");

    EXPECT_TRUE(result.success);
    EXPECT_EQ(result.previous_client_id, "tertiary");

    //
    // Admin preempts primary while primary keeps renewing.
    //
    EXPECT_TRUE(acquire("admin").success);
    EXPECT_FALSE(renew("primary"));
    EXPECT_EQ(holder(), "admin");
}

TEST_F(ControlManagerTest, AfterAdminReleasesTheWaitingHighestTierWins) {
    EXPECT_TRUE(acquire("admin").success);

    for (const char* client_id : {"tertiary", "secondary", "primary"}) {
        EXPECT_FALSE(acquire(client_id).success);
    }

    EXPECT_TRUE(manager->release("admin").success);

    EXPECT_FALSE(acquire("tertiary").success);
    EXPECT_FALSE(acquire("secondary").success);
    EXPECT_TRUE(acquire("primary").success);
}

// ================================================================
// Startup grace
// ================================================================

TEST_F(ControlManagerTest, NobodyGetsTheLeaseDuringStartupGrace) {
    manager = make_manager(5.0);

    const AcquireResult result = acquire("admin");

    EXPECT_FALSE(result.success);
    ASSERT_TRUE(result.retry_after.has_value());
    EXPECT_NEAR(*result.retry_after, 5.0, 1e-6);
    EXPECT_EQ(holder(), std::nullopt);
}

TEST_F(ControlManagerTest, AfterStartupGraceTheHighestClientTryingWins) {
    manager = make_manager(5.0);

    //
    // Clients reconnect lowest first, all inside the grace.
    //
    EXPECT_FALSE(acquire("tertiary").success);

    clock.advance(1.5);
    EXPECT_FALSE(acquire("secondary").success);

    clock.advance(1.5);
    EXPECT_FALSE(acquire("primary").success);

    //
    // Grace over: the lower tiers asking first are refused because
    // primary tried 2 s ago; primary gets it.
    //
    clock.advance(2.0);

    EXPECT_FALSE(acquire("tertiary").success);
    EXPECT_FALSE(acquire("secondary").success);
    EXPECT_TRUE(acquire("primary").success);
}

TEST_F(ControlManagerTest, StartupGraceOfZeroHandsOutControlAtOnce) {
    EXPECT_TRUE(acquire("tertiary").success);
}

// ================================================================
// Not in the Python tests: the lease itself on the fake clock
// ================================================================

TEST_F(ControlManagerTest, LeaseExpiresExactlyAfterTheLeasePeriod) {
    EXPECT_TRUE(acquire("primary").success);

    clock.advance(LEASE - 0.001);
    EXPECT_EQ(holder(), "primary");

    clock.advance(0.001);
    EXPECT_EQ(holder(), std::nullopt);
}

TEST_F(ControlManagerTest, RenewKeepsAcquiredAtAndMovesExpiry) {
    const auto acquired = acquire("primary").lease;

    ASSERT_TRUE(acquired.has_value());

    clock.advance(1.0);

    const RenewResult renewed = manager->renew("primary", PRIORITY.at("primary"));

    ASSERT_TRUE(renewed.success);
    EXPECT_EQ(renewed.lease->acquired_at, acquired->acquired_at);
    EXPECT_EQ(renewed.lease->expires_at, add_seconds(acquired->expires_at, 1.0));

    clock.advance(LEASE - 0.5);
    EXPECT_EQ(holder(), "primary");
}

TEST_F(ControlManagerTest, OnlyTheHolderOwnsControl) {
    EXPECT_FALSE(manager->owns_control("primary"));
    EXPECT_TRUE(acquire("primary").success);
    EXPECT_TRUE(manager->owns_control("primary"));
    EXPECT_FALSE(manager->owns_control("secondary"));
}

}  // namespace
}  // namespace gw
