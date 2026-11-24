#include "security/client_registry.hpp"

#include <fstream>
#include <sstream>

#include <nlohmann/json.hpp>

#include "util/sha256.hpp"
#include "util/text.hpp"

namespace gw {

namespace {

constexpr const char* INLINE_SOURCE = "GATEWAY_AUTH_CLIENTS";

//
// ordered_json keeps the clients in the order given.
//
nlohmann::ordered_json read_file(
    const std::string& filename
) {
    std::ifstream file(filename, std::ios::binary);

    if (!file) {
        throw RegistryError("Auth clients file not found: " + filename);
    }

    std::ostringstream content;

    content << file.rdbuf();

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

    return data;
}

std::vector<std::string> split(
    const std::string& text,
    char separator
) {
    std::vector<std::string> parts;
    std::string part;
    std::istringstream stream(text);

    while (std::getline(stream, part, separator)) {
        parts.push_back(part);
    }

    //
    // getline drops a trailing empty field ("a:b:" gives two).
    //
    if (!text.empty() && text.back() == separator) {
        parts.emplace_back();
    }

    return parts;
}

//
// The one-line form (see client_registry.hpp) as the file's JSON,
// so both go through the same checks.
//
nlohmann::ordered_json parse_inline(
    const std::string& text
) {
    nlohmann::ordered_json data = nlohmann::ordered_json::object();

    std::istringstream stream(text);
    std::string client;

    while (stream >> client) {
        const std::vector<std::string> fields = split(client, ':');

        const auto invalid = [&] {
            return RegistryError(
                format(
                    "Auth client '%s' in %s must be id:control_priority:permissions:token_hash",
                    fields.empty() ? "" : fields[0].c_str(),
                    INLINE_SOURCE
                )
            );
        };

        if (fields.size() != 4 || fields[0].empty() || fields[3].empty()) {
            throw invalid();
        }

        long long priority = 0;
        std::size_t used = 0;

        try {
            priority = std::stoll(fields[1], &used);

        } catch (const std::exception&) {
            throw invalid();
        }

        if (used != fields[1].size()) {
            throw invalid();
        }

        if (data.contains(fields[0])) {
            throw RegistryError(
                format("Auth client '%s' is listed twice in %s", fields[0].c_str(), INLINE_SOURCE)
            );
        }

        std::vector<std::string> permissions;

        for (const std::string& permission : split(fields[2], ',')) {
            if (permission.empty()) {
                throw invalid();
            }

            permissions.push_back(permission);
        }

        data[fields[0]] = {
            {"token_hash", fields[3]},
            {"permissions", permissions},
            {"control_priority", priority},
        };
    }

    return data;
}

}  // namespace

ClientRegistry::ClientRegistry(
    const std::string& filename,
    const std::string& inline_clients
) {
    const bool from_inline = !inline_clients.empty();

    const nlohmann::ordered_json data = from_inline ? parse_inline(inline_clients) : read_file(filename);

    const std::string source = from_inline ? INLINE_SOURCE : filename;

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
                    source.c_str()
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
        throw RegistryError("No auth clients in " + source);
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
