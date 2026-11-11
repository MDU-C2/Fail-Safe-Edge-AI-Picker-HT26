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

class RegistryError : public std::runtime_error {
public:
    using std::runtime_error::runtime_error;
};

class ClientRegistry {
public:
    explicit ClientRegistry(
        const std::string& filename
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
