#pragma once

#include <map>
#include <mutex>
#include <optional>
#include <string>

#include <nlohmann/json.hpp>

#include "rws/errors.hpp"

namespace gw::rws {

// ================================================================
// Robot Web Services client
// ================================================================
//
// RWS 1.0 on an IRC5 controller (RobotWare 6.16), as rws/client.py:
// HTTP digest auth, one session (cookies) shared by all calls, every
// call serialised by one lock, "json=1" on every request. Calls
// block; they run on the request's own connection thread.
//

struct Timeout {
    double connect_seconds;
    double read_seconds;
};

using Params = std::map<std::string, std::string>;

struct Reply {
    long status = 0;
    std::string body;
};

class RwsClient {
public:
    RwsClient(
        std::string host,
        long long port,
        std::string user,
        std::string password,
        double timeout_seconds
    );

    ~RwsClient();

    RwsClient(const RwsClient&) = delete;
    RwsClient& operator=(const RwsClient&) = delete;

    //
    // One request. Throws RwsError when the controller cannot be
    // reached or answers with an error status.
    //
    Reply request(
        const std::string& method,
        const std::string& path,
        const Params& params = {},
        const std::optional<std::string>& body = std::nullopt,
        const std::optional<std::string>& content_type = std::nullopt,
        const std::optional<Timeout>& timeout = std::nullopt
    );

    //
    // _embedded._state of a GET.
    //
    nlohmann::json get_state(
        const std::string& path,
        const Params& params = {},
        const std::optional<Timeout>& timeout = std::nullopt
    );

    //
    // A form-encoded POST (RWS refuses a POST without that content
    // type, even an empty one).
    //
    void post(
        const std::string& path,
        const Params& params = {},
        const Params& data = {}
    );

    // ------------------------------------------------------------
    // Controller and RAPID state
    // ------------------------------------------------------------

    std::string controller_state(
        const std::optional<Timeout>& timeout = std::nullopt
    );

    std::string operation_mode(
        const std::optional<Timeout>& timeout = std::nullopt
    );

    std::string execution_state(
        const std::optional<Timeout>& timeout = std::nullopt
    );

    const std::string& base_url() const {
        return base_;
    }

    //
    // For multi-call sequences that must not interleave with other
    // clients' calls (mastership).
    //
    std::recursive_mutex& lock() {
        return mutex_;
    }

private:
    std::string base_;
    std::string user_password_;
    double timeout_seconds_;

    std::recursive_mutex mutex_;

    void* curl_ = nullptr;
};

//
// "a=1&b=2", percent-encoded.
//
std::string encode_form(
    const Params& values
);

}  // namespace gw::rws
