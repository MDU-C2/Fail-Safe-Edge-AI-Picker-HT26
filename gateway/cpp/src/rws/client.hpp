#pragma once

#include <map>
#include <mutex>
#include <optional>
#include <string>
#include <vector>

#include <nlohmann/json.hpp>

#include "rws/controller.hpp"
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

class RwsClient : public Controller {
public:
    RwsClient(
        std::string host,
        long long port,
        std::string user,
        std::string password,
        double timeout_seconds
    );

    ~RwsClient() override;

    RwsClient(const RwsClient&) = delete;
    RwsClient& operator=(const RwsClient&) = delete;

    // ------------------------------------------------------------
    // HTTP
    // ------------------------------------------------------------

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

    //
    // Holds the controller's mastership (and this client's lock)
    // for its lifetime, as client.py's mastership() context.
    //
    class Mastership {
    public:
        explicit Mastership(
            RwsClient& client
        );

        ~Mastership() noexcept(false);

        Mastership(const Mastership&) = delete;
        Mastership& operator=(const Mastership&) = delete;

    private:
        RwsClient& client_;

        std::lock_guard<std::recursive_mutex> lock_;

        int exceptions_;
    };

    // ------------------------------------------------------------
    // Motion system
    // ------------------------------------------------------------

    nlohmann::json mechunit(
        const std::string& unit
    );

    nlohmann::json robtarget(
        const std::string& unit,
        const std::optional<std::string>& tool,
        const std::optional<std::string>& wobj,
        const std::optional<std::string>& coordinate
    );

    nlohmann::json jointtarget(
        const std::string& unit
    );

    //
    // SmartGripper finger position of side "L" or "R" in mm, from
    // the gripper's own signal (0.1 mm). A controller without the
    // SmartGripper (RobotStudio) answers 404.
    //
    double gripper_position(
        const std::string& side
    );

    // ------------------------------------------------------------
    // Controller and RAPID state (Controller)
    // ------------------------------------------------------------

    std::string controller_state() override;
    std::string operation_mode() override;
    std::string execution_state() override;

    std::string controller_state(
        const Timeout& timeout
    );

    std::string operation_mode(
        const Timeout& timeout
    );

    std::string execution_state(
        const Timeout& timeout
    );

    nlohmann::json tasks() override;

    std::vector<std::string> program_modules(
        const std::string& task
    ) override;

    std::string read_symbol(
        const std::string& task,
        const std::string& name
    ) override;

    void write_symbol(
        const std::string& task,
        const std::string& name,
        const std::string& value
    ) override;

    // ------------------------------------------------------------
    // RAPID program control
    // ------------------------------------------------------------

    //
    // Writes the source (ISO-8859-1 bytes) to $HOME/<name>.mod on
    // the controller and loads it, replacing a module of that name.
    //
    void load_module(
        const std::string& task,
        const std::string& name,
        const std::string& latin1_source
    );

    void unload_module(
        const std::string& task,
        const std::string& name
    );

    void start(
        bool reset_program_pointer
    );

    void stop();

    const std::string& base_url() const {
        return base_;
    }

private:
    std::string state_field(
        const std::string& path,
        const char* field,
        const std::optional<Timeout>& timeout
    );

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
