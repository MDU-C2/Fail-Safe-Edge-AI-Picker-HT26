#pragma once

#include <string>
#include <vector>

#include <nlohmann/json.hpp>

namespace gw::rws {

// ================================================================
// Controller interface
// ================================================================
//
// What RAPID status and the RWS motion handshake need from the
// controller. RwsClient implements it over Robot Web Services; the
// unit tests implement it with fake controllers. Every call may
// throw RwsError.
//

class Controller {
public:
    virtual ~Controller() = default;

    //
    // "running" / "stopped"
    //
    virtual std::string execution_state() = 0;

    virtual std::string controller_state() = 0;

    virtual std::string operation_mode() = 0;

    //
    // The _state entries of /rw/rapid/tasks: name, taskstate,
    // excstate and (not for background tasks) active.
    //
    virtual nlohmann::json tasks() = 0;

    //
    // Names of the program modules (not system modules) of a task.
    //
    virtual std::vector<std::string> program_modules(
        const std::string& task
    ) = 0;

    //
    // A RAPID variable as RAPID text, e.g. "TRUE", "[1,2,3]" or
    // "\"text\"" (strings keep their quotes). A missing variable is
    // RwsError with status 400.
    //
    virtual std::string read_symbol(
        const std::string& task,
        const std::string& name
    ) = 0;

    virtual void write_symbol(
        const std::string& task,
        const std::string& name,
        const std::string& value
    ) = 0;
};

}  // namespace gw::rws
