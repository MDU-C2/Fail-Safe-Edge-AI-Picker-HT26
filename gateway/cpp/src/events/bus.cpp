#include "events/bus.hpp"

#include <algorithm>

#include "util/text.hpp"

namespace gw {

bool matches_subscription(
    const std::string& event_type,
    const std::string& subscription
) {
    return event_type == subscription
        || starts_with(event_type, subscription + ".");
}

// ================================================================
// Connection
// ================================================================

EventConnection::EventConnection(
    Identity identity,
    Send send
)
    : identity_(std::move(identity)),
      send_(std::move(send)) {}

std::set<std::string> EventConnection::subscribe(
    const std::vector<std::string>& events
) {
    const std::lock_guard lock(mutex_);

    subscriptions_.insert(events.begin(), events.end());

    return subscriptions_;
}

std::set<std::string> EventConnection::unsubscribe(
    const std::vector<std::string>& events
) {
    const std::lock_guard lock(mutex_);

    for (const auto& event : events) {
        subscriptions_.erase(event);
    }

    return subscriptions_;
}

bool EventConnection::matches(
    const std::string& event_type
) const {
    const std::lock_guard lock(mutex_);

    return std::any_of(
        subscriptions_.begin(),
        subscriptions_.end(),
        [&](const std::string& subscription) {
            return matches_subscription(event_type, subscription);
        }
    );
}

bool EventConnection::send(
    const std::string& text
) {
    return send_(text);
}

// ================================================================
// Bus
// ================================================================

std::shared_ptr<EventConnection> EventBus::connect(
    Identity identity,
    EventConnection::Send send
) {
    auto connection = std::make_shared<EventConnection>(std::move(identity), std::move(send));

    const std::lock_guard lock(mutex_);

    connections_.push_back(connection);

    return connection;
}

void EventBus::disconnect(
    const std::shared_ptr<EventConnection>& connection
) {
    const std::lock_guard lock(mutex_);

    connections_.erase(
        std::remove(connections_.begin(), connections_.end(), connection),
        connections_.end()
    );
}

std::vector<std::shared_ptr<EventConnection>> EventBus::snapshot() const {
    const std::lock_guard lock(mutex_);

    return connections_;
}

std::size_t EventBus::connection_count() const {
    const std::lock_guard lock(mutex_);

    return connections_.size();
}

void EventBus::publish(
    const std::string& event_type,
    const http::Json& data
) {
    const auto connections = snapshot();

    if (connections.empty()) {
        return;
    }

    const std::string text = http::to_text(http::Json{{"type", event_type}, {"data", data}});

    for (const auto& connection : connections) {
        if (connection->matches(event_type) && !connection->send(text)) {
            disconnect(connection);
        }
    }
}

void EventBus::send_to_client(
    const std::string& client_id,
    const http::Json& event
) {
    const std::string text = http::to_text(event);

    for (const auto& connection : snapshot()) {
        if (connection->identity().client_id == client_id && !connection->send(text)) {
            disconnect(connection);
        }
    }
}

}  // namespace gw
