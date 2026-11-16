#pragma once

#include <memory>
#include <mutex>
#include <string>

#include "config/settings.hpp"
#include "http/message.hpp"

namespace gw {

class EventBus;

// ================================================================
// Robot service (legacy socket path)
// ================================================================
//
// gateway/robot/service.py: "mock" tracks idle/running and contacts
// nothing, "tcp" talks to the team's CommServer. POST /robot/command
// (execute) follows in a later step.
//

class RobotService {
public:
    virtual ~RobotService() = default;

    //
    // {"connected", "state"}
    //
    virtual http::Json status() const = 0;
};

class MockRobotService : public RobotService {
public:
    http::Json status() const override;

private:
    mutable std::mutex mutex_;

    std::string state_ = "idle";
};

class TcpRobotService : public RobotService {
public:
    TcpRobotService(
        std::string host,
        long long port
    );

    http::Json status() const override;

private:
    std::string host_;
    long long port_;
};

std::unique_ptr<RobotService> create_robot_service(
    const Settings& settings
);

}  // namespace gw
