#pragma once

#include <optional>
#include <string>

#include "http/message.hpp"
#include "security/client_registry.hpp"
#include "security/identity.hpp"

namespace gw {

class ControlManager;

// ================================================================
// Authentication
// ================================================================
//
// What gateway/security/authentication.py and require_control do:
//
//   development  the client named by X-Dev-Client (default: the
//                highest-priority client), no token
//   strict       Authorization: Bearer <token>, looked up by hash
//
// Failures throw http::HttpError: 401 (with WWW-Authenticate:
// Bearer in strict mode), 403 for a missing permission, 409 for a
// missing control lease.
//

class Authenticator {
public:
    Authenticator(
        const ClientRegistry& registry,
        bool development,
        const ControlManager& control
    );

    Identity identify(
        const http::Request& request
    ) const;

    Identity require_permission(
        const http::Request& request,
        const std::string& permission
    ) const;

    //
    // The permission and the active control lease.
    //
    Identity require_control(
        const http::Request& request,
        const std::string& permission
    ) const;

    //
    // Development mode: the client of that id (default the highest
    // priority), or nothing. Also used by the events WebSocket.
    //
    std::optional<Identity> development_identity(
        const std::optional<std::string>& client_id
    ) const;

    //
    // Strict mode: the client of the "Authorization: Bearer <token>"
    // header, or nothing for a missing, malformed or unknown token.
    // Used by the events WebSocket.
    //
    std::optional<Identity> bearer_identity(
        const http::Request& request
    ) const;

    bool development() const {
        return development_;
    }

private:
    const ClientRegistry& registry_;

    bool development_;

    const ControlManager& control_;
};

}  // namespace gw
