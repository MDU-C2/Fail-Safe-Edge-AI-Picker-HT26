#include "security/auth.hpp"

#include "control/manager.hpp"
#include "util/text.hpp"

namespace gw {

Authenticator::Authenticator(
    const ClientRegistry& registry,
    bool development,
    const ControlManager& control
)
    : registry_(registry),
      development_(development),
      control_(control) {}

std::optional<Identity> Authenticator::development_identity(
    const std::optional<std::string>& client_id
) const {
    //
    // An empty id counts as none, like Python's `client_id or default`.
    //
    const std::string& id = client_id && !client_id->empty()
        ? *client_id
        : registry_.default_client_id();

    return registry_.identity(id, false);
}

Identity Authenticator::identify(
    const http::Request& request
) const {
    // ------------------------------------------------------------
    // Development mode
    // ------------------------------------------------------------

    if (development_) {
        std::optional<std::string> client_id;

        if (const auto header = request.header("X-Dev-Client")) {
            client_id = std::string(*header);
        }

        auto identity = development_identity(client_id);

        if (!identity) {
            throw http::HttpError(401, "Unknown development client");
        }

        return *identity;
    }

    // ------------------------------------------------------------
    // Strict mode
    // ------------------------------------------------------------

    //
    // As FastAPI's HTTPBearer(auto_error=False): anything but
    // "Bearer <credentials>" counts as no token.
    //
    std::optional<std::string> token;

    if (const auto header = request.header("Authorization")) {
        const std::string_view value = trim(*header);
        const auto space = value.find(' ');

        if (space != std::string_view::npos && iequals(value.substr(0, space), "bearer")) {
            const std::string_view credentials = trim(value.substr(space + 1));

            if (!credentials.empty()) {
                token = std::string(credentials);
            }
        }
    }

    const http::Headers challenge = {{"WWW-Authenticate", "Bearer"}};

    if (!token) {
        throw http::HttpError(401, "Missing Bearer token", challenge);
    }

    auto identity = registry_.authenticate(*token);

    if (!identity) {
        throw http::HttpError(401, "Invalid Bearer token", challenge);
    }

    return *identity;
}

Identity Authenticator::require_permission(
    const http::Request& request,
    const std::string& permission
) const {
    Identity identity = identify(request);

    if (!identity.can(permission)) {
        throw http::HttpError(403, "Missing permission: " + permission);
    }

    return identity;
}

Identity Authenticator::require_control(
    const http::Request& request,
    const std::string& permission
) const {
    Identity identity = require_permission(request, permission);

    if (!control_.owns_control(identity.client_id)) {
        throw http::HttpError(409, "Client does not own the active control lease");
    }

    return identity;
}

}  // namespace gw
