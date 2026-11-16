#include "robot/service.hpp"

namespace gw {

http::Json MockRobotService::status() const {
    const std::lock_guard lock(mutex_);

    return http::Json{
        {"connected", true},
        {"state", state_},
    };
}

TcpRobotService::TcpRobotService(
    std::string host,
    long long port
)
    : host_(std::move(host)),
      port_(port) {}

http::Json TcpRobotService::status() const {
    //
    // CommServer has no status without a job; like the Python
    // gateway, report it unknown.
    //
    return http::Json{
        {"connected", false},
        {"state", "unknown"},
    };
}

std::unique_ptr<RobotService> create_robot_service(
    const Settings& settings
) {
    if (settings.robot_provider == "mock") {
        return std::make_unique<MockRobotService>();
    }

    return std::make_unique<TcpRobotService>(settings.robot_host, settings.robot_port);
}

}  // namespace gw
