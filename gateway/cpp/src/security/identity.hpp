#pragma once

#include <set>
#include <string>

namespace gw {

struct Identity {
    std::string client_id;

    bool authenticated = false;

    std::set<std::string> permissions;

    long long control_priority = 0;

    //
    // "*" grants every permission.
    //
    bool can(
        const std::string& permission
    ) const {
        return permissions.count("*") > 0 || permissions.count(permission) > 0;
    }
};

}  // namespace gw
