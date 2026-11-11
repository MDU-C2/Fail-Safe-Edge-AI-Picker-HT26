#pragma once

#include <functional>
#include <memory>
#include <mutex>
#include <set>
#include <string>
#include <vector>

#include "http/message.hpp"
#include "security/identity.hpp"

namespace gw {

// ================================================================
// Event bus
// ================================================================
//
// gateway/events/manager.py without the transport: connections
// register a send function, publish() delivers {"type", "data"} to
// every connection with a matching subscription (dotted prefix:
// "control" receives "control.acquired"), and send_to_client()
// reaches every connection of one client regardless of its
// subscriptions. A connection whose send fails is dropped.
//

class EventConnection {
public:
    using Send = std::function<bool(const std::string& text)>;

    EventConnection(
        Identity identity,
        Send send
    );

    const Identity& identity() const {
        return identity_;
    }

    std::set<std::string> subscribe(
        const std::vector<std::string>& events
    );

    std::set<std::string> unsubscribe(
        const std::vector<std::string>& events
    );

    bool matches(
        const std::string& event_type
    ) const;

    bool send(
        const std::string& text
    );

private:
    Identity identity_;
    Send send_;

    mutable std::mutex mutex_;

    std::set<std::string> subscriptions_;
};

class EventBus {
public:
    std::shared_ptr<EventConnection> connect(
        Identity identity,
        EventConnection::Send send
    );

    void disconnect(
        const std::shared_ptr<EventConnection>& connection
    );

    void publish(
        const std::string& event_type,
        const http::Json& data
    );

    void send_to_client(
        const std::string& client_id,
        const http::Json& event
    );

    std::size_t connection_count() const;

private:
    std::vector<std::shared_ptr<EventConnection>> snapshot() const;

    mutable std::mutex mutex_;

    std::vector<std::shared_ptr<EventConnection>> connections_;
};

bool matches_subscription(
    const std::string& event_type,
    const std::string& subscription
);

}  // namespace gw
