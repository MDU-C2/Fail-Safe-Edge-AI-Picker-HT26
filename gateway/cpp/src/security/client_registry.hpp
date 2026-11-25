#pragma once

#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

#include "security/identity.hpp"

namespace gw {

// ================================================================
// Client registry
// ================================================================
//
// The clients of gateway/auth_clients.json: id -> token_hash
// (SHA-256 hex of the token), permissions and control_priority.
// Loaded once at startup; a missing or malformed file throws
// RegistryError so the gateway does not start.
//
// GATEWAY_AUTH_CLIENTS, when set, replaces the file (C++ only): the
// same clients on one line, so they fit a WeOS app environment
// value, which cannot hold JSON's quotes. Clients are separated by
// spaces, each one id:control_priority:permissions:token_hash with
// the permissions separated by commas (may be empty), e.g.
//
//   admin:1000:*:9f86d0... diagnostics:10:robot.read,camera.read:5e88...
//

class RegistryError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

class ClientRegistry {
public:
    //
    // From inline_clients if it is not empty, else from the file.
    //
    explicit ClientRegistry(
        const std::string& filename,
        const std::string& inline_clients = ""
    );

    //
    // The client with the highest control priority.
    //
    const std::string& default_client_id() const;

    std::optional<Identity> identity(
        const std::string& client_id,
        bool authenticated
    ) const;

    //
    // The client whose token hashes to its token_hash.
    //
    std::optional<Identity> authenticate(
        const std::string& token
    ) const;

private:
    struct Client {
        std::string id;
        std::string token_hash;
        std::vector<std::string> permissions;
        long long control_priority = 0;
    };

    //
    // In file order, so the default (first of the highest
    // priority) matches Python's max().
    //
    std::vector<Client> clients_;

    std::string default_client_id_;
};

}  // namespace gw
