#pragma once

#include <cstdint>
#include <exception>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <string_view>
#include <utility>
#include <vector>

#include <nlohmann/json.hpp>

namespace gw::http {

// ================================================================
// JSON
// ================================================================

//
// Keeps keys in insertion order, so responses list their fields in
// the same order as the Python gateway's Pydantic models.
//
using Json = nlohmann::ordered_json;

//
// Compact, UTF-8 (invalid bytes replaced), like FastAPI's JSON.
//
std::string to_text(
    const Json& value
);

using Bytes = std::vector<std::uint8_t>;

using Headers = std::vector<std::pair<std::string, std::string>>;

// ================================================================
// Request
// ================================================================

struct Request {
    std::string method;

    //
    // Decoded path, without the query string.
    //
    std::string path;

    std::map<std::string, std::string> query;

    Headers headers;

    std::string body;

    //
    // {name} parts of the matched route.
    //
    std::map<std::string, std::string> params;

    //
    // Case-insensitive header lookup.
    //
    std::optional<std::string_view> header(
        std::string_view name
    ) const;

    std::optional<std::string> query_value(
        const std::string& name
    ) const;

    const std::string& param(
        const std::string& name
    ) const;
};

// ================================================================
// Response
// ================================================================

struct Response {
    int status = 200;

    std::string content_type = "application/json";

    Headers headers;

    std::string body;

    //
    // Large bodies (camera frames) are sent from a shared buffer
    // without copying; when set, it is sent instead of body.
    //
    std::shared_ptr<const Bytes> shared_body;

    static Response json(
        const Json& value,
        int status = 200
    );

    std::size_t body_size() const;

    const void* body_data() const;
};

// ================================================================
// Errors
// ================================================================

//
// Thrown by handlers; sent as FastAPI's {"detail": ...}.
//
class HttpError : public std::exception {
public:
    HttpError(
        int status,
        Json detail,
        Headers headers = {}
    );

    const char* what() const noexcept override;

    int status;
    Json detail;
    Headers headers;

private:
    std::string message_;
};

Response error_response(
    int status,
    const Json& detail,
    const Headers& headers = {}
);

//
// A 422 in FastAPI's shape, for one problem:
// {"detail": [{"type", "loc", "msg", "input"}]}.
//
HttpError validation_error(
    std::vector<Json> location,
    std::string message,
    std::string type = "value_error",
    Json input = nullptr
);

//
// The request body as JSON; 422 if it is not valid JSON.
//
Json parse_json_body(
    const Request& request
);

}  // namespace gw::http
