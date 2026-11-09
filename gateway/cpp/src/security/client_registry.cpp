#include "security/client_registry.hpp"

#include <fstream>
#include <sstream>

#include <nlohmann/json.hpp>

#include "util/sha256.hpp"
#include "util/text.hpp"

namespace gw {

ClientRegistry::ClientRegistry(
    const std::string& filename
) {
    std::ifstream file(filename, std::ios::binary);

    if (!file) {
        throw RegistryError("Auth clients file not found: " + filename);
    }

    std::ostringstream content;

    content << file.rdbuf();

    //
    // ordered_json keeps the file order of the clients.
    //
    nlohmann::ordered_json data;

    try {
        data = nlohmann::ordered_json::parse(content.str());

    } catch (const nlohmann::json::parse_error& error) {
        throw RegistryError(
            format("Auth clients file is not valid JSON: %s: %s", filename.c_str(), error.what())
        );
    }

    if (!data.is_object()) {
        throw RegistryError("Auth clients file must contain a JSON object: " + filename);
    }

    for (const auto& [client_id, client] : data.items()) {
        const bool valid =
            client.is_object()
            && client.contains("token_hash")
            && client["token_hash"].is_string()
            && client.contains("permissions")
            && client["permissions"].is_array()
            && client.contains("control_priority")
            && client["control_priority"].is_number_integer();

        bool permissions_valid = valid;

        if (valid) {
            for (const auto& permission : client["permissions"]) {
                permissions_valid = permissions_valid && permission.is_string();
            }
        }

        if (!permissions_valid) {
            throw RegistryError(
                format(
                    "Auth client '%s' in %s must have 'token_hash', 'permissions' and 'control_priority'",
                    client_id.c_str(),
                    filename.c_str()
                )
            );
        }

        clients_.push_back(
            Client{
                client_id,
                client["token_hash"].get<std::string>(),
                client["permissions"].get<std::vector<std::string>>(),
                client["control_priority"].get<long long>(),
            }
        );
    }

    if (clients_.empty()) {
        throw RegistryError("Auth clients file has no clients: " + filename);
    }

    const Client* highest = &clients_.front();

    for (const Client& client : clients_) {
        if (client.control_priority > highest->control_priority) {
            highest = &client;
        }
    }

    default_client_id_ = highest->id;
}

const std::string& ClientRegistry::default_client_id() const {
    return default_client_id_;
}

std::optional<Identity> ClientRegistry::identity(
    const std::string& client_id,
    bool authenticated
) const {
    for (const Client& client : clients_) {
        if (client.id == client_id) {
            return Identity{
                client.id,
                authenticated,
                std::set<std::string>(client.permissions.begin(), client.permissions.end()),
                client.control_priority,
            };
        }
    }

    return std::nullopt;
}

std::optional<Identity> ClientRegistry::authenticate(
    const std::string& token
) const {
    const std::string token_hash = sha256_hex(token);

    for (const Client& client : clients_) {
        if (constant_time_equal(token_hash, client.token_hash)) {
            return identity(client.id, true);
        }
    }

    return std::nullopt;
}

}  // namespace gw
