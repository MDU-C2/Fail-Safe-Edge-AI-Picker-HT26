#include "rws/client.hpp"

#include <exception>

#include <curl/curl.h>

#include "util/text.hpp"

namespace gw::rws {

namespace {

//
// Arguments for starting RAPID execution, the same as the
// FlexPendant's start button.
//
const Params START_ARGUMENTS = {
    {"regain", "continue"},
    {"execmode", "continue"},
    {"cycle", "forever"},
    {"condition", "none"},
    {"stopatbp", "disabled"},
    {"alltaskbytsp", "false"},
};

//
// curl_global_init is not thread-safe; do it once before any client.
//
void global_init() {
    static const bool initialised = []() {
        curl_global_init(CURL_GLOBAL_DEFAULT);

        return true;
    }();

    (void)initialised;
}

std::size_t collect(
    char* data,
    std::size_t size,
    std::size_t count,
    void* user
) {
    static_cast<std::string*>(user)->append(data, size * count);

    return size * count;
}

//
// What a client needs to know about a failed connection, as
// _connection_problem in client.py.
//
std::string connection_problem(
    CURLcode code,
    CURL* curl
) {
    if (code == CURLE_OPERATION_TIMEDOUT) {
        //
        // Connected or not decides which timeout it was.
        //
        curl_off_t connect_time = 0;

        curl_easy_getinfo(curl, CURLINFO_CONNECT_TIME_T, &connect_time);

        return connect_time > 0 ? "no answer in time" : "connection timed out";
    }

    if (code == CURLE_COULDNT_CONNECT || code == CURLE_COULDNT_RESOLVE_HOST) {
        return "connection failed (refused or no route)";
    }

    return curl_easy_strerror(code);
}

}  // namespace

std::string encode_form(
    const Params& values
) {
    std::string text;

    for (const auto& [key, value] : values) {
        if (!text.empty()) {
            text += '&';
        }

        text += url_encode(key);
        text += '=';
        text += url_encode(value);
    }

    return text;
}

// ================================================================
// HTTP
// ================================================================

RwsClient::RwsClient(
    std::string host,
    long long port,
    std::string user,
    std::string password,
    double timeout_seconds
)
    : base_(format("http://%s:%lld", host.c_str(), port)),
      user_password_(user + ":" + password),
      timeout_seconds_(timeout_seconds) {
    global_init();

    curl_ = curl_easy_init();

    CURL* curl = static_cast<CURL*>(curl_);

    curl_easy_setopt(curl, CURLOPT_HTTPAUTH, CURLAUTH_DIGEST);
    curl_easy_setopt(curl, CURLOPT_USERPWD, user_password_.c_str());

    //
    // An empty cookie file switches on the in-memory cookie engine,
    // so the RWS session cookie is kept like requests.Session does.
    //
    curl_easy_setopt(curl, CURLOPT_COOKIEFILE, "");

    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, collect);
}

RwsClient::~RwsClient() {
    curl_easy_cleanup(static_cast<CURL*>(curl_));
}

Reply RwsClient::request(
    const std::string& method,
    const std::string& path,
    const Params& params,
    const std::optional<std::string>& body,
    const std::optional<std::string>& content_type,
    const std::optional<Timeout>& timeout
) {
    //
    // json=1 makes RWS answer in JSON, including the reason in
    // error responses, instead of XHTML.
    //
    Params query = params;

    query["json"] = "1";

    const std::string url = base_ + path + "?" + encode_form(query);

    const Timeout limits = timeout.value_or(Timeout{timeout_seconds_, timeout_seconds_});

    const std::lock_guard lock(mutex_);

    CURL* curl = static_cast<CURL*>(curl_);

    Reply reply;

    curl_slist* headers = nullptr;

    if (content_type) {
        headers = curl_slist_append(headers, ("Content-Type: " + *content_type).c_str());
    }

    //
    // No "Expect: 100-continue" round trip for bodies.
    //
    headers = curl_slist_append(headers, "Expect:");

    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &reply.body);

    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, static_cast<long>(limits.connect_seconds * 1000));

    //
    // requests' read timeout is per read; curl's is for the whole
    // transfer, which also covers connecting.
    //
    curl_easy_setopt(
        curl,
        CURLOPT_TIMEOUT_MS,
        static_cast<long>((limits.connect_seconds + limits.read_seconds) * 1000)
    );

    //
    // Reset the method from the previous call first.
    //
    curl_easy_setopt(curl, CURLOPT_HTTPGET, 1L);
    curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, nullptr);

    if (method == "POST" || method == "PUT") {
        const std::string& data = body ? *body : std::string();

        curl_easy_setopt(curl, CURLOPT_POST, 1L);
        curl_easy_setopt(curl, CURLOPT_POSTFIELDSIZE_LARGE, static_cast<curl_off_t>(data.size()));
        curl_easy_setopt(curl, CURLOPT_COPYPOSTFIELDS, data.c_str());

        if (method == "PUT") {
            curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, "PUT");
        }
    } else if (method != "GET") {
        curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, method.c_str());
    }

    const CURLcode code = curl_easy_perform(curl);

    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, nullptr);
    curl_slist_free_all(headers);

    if (code != CURLE_OK) {
        throw RwsError(
            std::nullopt,
            format(
                "Robot controller unreachable at %s: %s",
                base_.c_str(),
                connection_problem(code, curl).c_str()
            )
        );
    }

    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &reply.status);

    if (reply.status >= 400) {
        throw RwsError(
            static_cast<int>(reply.status),
            error_message(reply.body, format("HTTP %ld", reply.status))
        );
    }

    return reply;
}

nlohmann::json RwsClient::get_state(
    const std::string& path,
    const Params& params,
    const std::optional<Timeout>& timeout
) {
    const Reply reply = request("GET", path, params, std::nullopt, std::nullopt, timeout);

    const auto json = nlohmann::json::parse(reply.body, nullptr, false);

    if (
        !json.is_object()
        || !json.contains("_embedded")
        || !json["_embedded"].is_object()
        || !json["_embedded"].contains("_state")
        || !json["_embedded"]["_state"].is_array()
    ) {
        throw RwsError(
            static_cast<int>(reply.status),
            "Unexpected answer from the robot controller for " + path
        );
    }

    return json["_embedded"]["_state"];
}

void RwsClient::post(
    const std::string& path,
    const Params& params,
    const Params& data
) {
    request(
        "POST",
        path,
        params,
        encode_form(data),
        std::string("application/x-www-form-urlencoded")
    );
}

RwsClient::Mastership::Mastership(
    RwsClient& client
)
    : client_(client),
      lock_(client.mutex_),
      exceptions_(std::uncaught_exceptions()) {
    client_.post("/rw/mastership", {{"action", "request"}});
}

RwsClient::Mastership::~Mastership() noexcept(false) {
    try {
        client_.post("/rw/mastership", {{"action", "release"}});

    } catch (...) {
        //
        // Leaving because of another error: that one is the
        // interesting one, keep it.
        //
        if (std::uncaught_exceptions() > exceptions_) {
            return;
        }

        throw;
    }
}

namespace {

//
// The one entry of a _state list, or RwsError.
//
const nlohmann::json& first(
    const nlohmann::json& state,
    const std::string& path
) {
    if (state.empty() || !state[0].is_object()) {
        throw RwsError(502, "Robot controller sent no state for " + path);
    }

    return state[0];
}

std::string field(
    const nlohmann::json& object,
    const char* name
) {
    if (!object.contains(name)) {
        throw RwsError(502, std::string("Robot controller answer without ") + name);
    }

    const auto& value = object[name];

    return value.is_string() ? value.get<std::string>() : value.dump();
}

}  // namespace

// ================================================================
// Motion system
// ================================================================

nlohmann::json RwsClient::mechunit(
    const std::string& unit
) {
    const std::string path = "/rw/motionsystem/mechunits/" + unit;

    return first(get_state(path), path);
}

nlohmann::json RwsClient::robtarget(
    const std::string& unit,
    const std::optional<std::string>& tool,
    const std::optional<std::string>& wobj,
    const std::optional<std::string>& coordinate
) {
    Params params;

    if (tool) {
        params["tool"] = *tool;
    }

    if (wobj) {
        params["wobj"] = *wobj;
    }

    if (coordinate) {
        params["coordinate"] = *coordinate;
    }

    const std::string path = "/rw/motionsystem/mechunits/" + unit + "/robtarget";

    return first(get_state(path, params), path);
}

nlohmann::json RwsClient::jointtarget(
    const std::string& unit
) {
    const std::string path = "/rw/motionsystem/mechunits/" + unit + "/jointtarget";

    return first(get_state(path), path);
}

double RwsClient::gripper_position(
    const std::string& side
) {
    const std::string path = "/rw/iosystem/signals/Hand_" + side + "/hand_ActualPosition_" + side;

    const std::string value = field(first(get_state(path), path), "lvalue");

    const auto number = parse_double(value);

    if (!number) {
        throw RwsError(502, "Robot controller sent a gripper position that is not a number: " + value);
    }

    return *number / 10;
}

// ================================================================
// Controller and RAPID state
// ================================================================

std::string RwsClient::state_field(
    const std::string& path,
    const char* name,
    const std::optional<Timeout>& timeout
) {
    return field(first(get_state(path, {}, timeout), path), name);
}

std::string RwsClient::controller_state() {
    return state_field("/rw/panel/ctrlstate", "ctrlstate", std::nullopt);
}

std::string RwsClient::operation_mode() {
    return state_field("/rw/panel/opmode", "opmode", std::nullopt);
}

std::string RwsClient::execution_state() {
    return state_field("/rw/rapid/execution", "ctrlexecstate", std::nullopt);
}

std::string RwsClient::controller_state(
    const Timeout& timeout
) {
    return state_field("/rw/panel/ctrlstate", "ctrlstate", timeout);
}

std::string RwsClient::operation_mode(
    const Timeout& timeout
) {
    return state_field("/rw/panel/opmode", "opmode", timeout);
}

std::string RwsClient::execution_state(
    const Timeout& timeout
) {
    return state_field("/rw/rapid/execution", "ctrlexecstate", timeout);
}

nlohmann::json RwsClient::tasks() {
    return get_state("/rw/rapid/tasks");
}

std::vector<std::string> RwsClient::program_modules(
    const std::string& task
) {
    std::vector<std::string> names;

    for (const auto& module : get_state("/rw/rapid/modules", {{"task", task}})) {
        if (module.is_object() && module.value("type", "") == "ProgMod" && module.contains("name")) {
            names.push_back(field(module, "name"));
        }
    }

    return names;
}

std::string RwsClient::read_symbol(
    const std::string& task,
    const std::string& name
) {
    const std::string path = "/rw/rapid/symbol/data/RAPID/" + task + "/" + name;

    return field(first(get_state(path), path), "value");
}

void RwsClient::write_symbol(
    const std::string& task,
    const std::string& name,
    const std::string& value
) {
    //
    // Works while RAPID runs and needs no mastership; the
    // controller refuses mastership while RAPID runs anyway.
    //
    post(
        "/rw/rapid/symbol/data/RAPID/" + task + "/" + name,
        {{"action", "set"}},
        {{"value", value}}
    );
}

// ================================================================
// RAPID program control
// ================================================================

void RwsClient::load_module(
    const std::string& task,
    const std::string& name,
    const std::string& latin1_source
) {
    //
    // The file is written to $HOME on the controller and then
    // loaded from there, replacing a module with the same name.
    //
    const std::string path = "$HOME/" + name + ".mod";

    const Mastership mastership(*this);

    request(
        "PUT",
        "/fileservice/" + path,
        {},
        latin1_source,
        std::string("text/plain;v=2.0")
    );

    post(
        "/rw/rapid/tasks/" + task,
        {{"action", "loadmod"}},
        {{"modulepath", path}, {"replace", "true"}}
    );
}

void RwsClient::unload_module(
    const std::string& task,
    const std::string& name
) {
    const Mastership mastership(*this);

    post(
        "/rw/rapid/tasks/" + task,
        {{"action", "unloadmod"}},
        {{"module", name}}
    );
}

void RwsClient::start(
    bool reset_program_pointer
) {
    const std::lock_guard lock(mutex_);

    if (reset_program_pointer) {
        const Mastership mastership(*this);

        post("/rw/rapid/execution", {{"action", "resetpp"}});
    }

    post("/rw/rapid/execution", {{"action", "start"}}, START_ARGUMENTS);
}

void RwsClient::stop() {
    post("/rw/rapid/execution", {{"action", "stop"}}, {{"stopmode", "stop"}});
}

}  // namespace gw::rws
