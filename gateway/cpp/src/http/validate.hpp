#pragma once

#include <initializer_list>
#include <optional>
#include <string>
#include <vector>

#include "http/message.hpp"

namespace gw::http {

// ================================================================
// Request validation (Pydantic's rules, FastAPI's 422)
// ================================================================
//
// Fields reads the values of one JSON object (a body, or the query
// or path parameters) with Pydantic v2's lax coercions: numbers may
// be numeric strings, booleans may be 0/1 or "true"/"yes"/"on"...
// Every problem is collected; check() throws them as one 422 in
// FastAPI's {"detail": [{type, loc, msg, input}]} shape.
//

struct Range {
    std::optional<double> ge;
    std::optional<double> gt;
    std::optional<double> le;
};

class Fields {
public:
    //
    // location: "body", "query" or "path".
    //
    Fields(
        Json values,
        std::string location
    );

    //
    // A finite number (FiniteFloat) in range; nothing if absent and
    // not required.
    //
    std::optional<double> number(
        const std::string& name,
        bool required,
        Range range = {}
    );

    //
    // One of the given strings (Literal); the default if absent.
    //
    std::optional<std::string> choice(
        const std::string& name,
        std::initializer_list<const char*> options,
        std::optional<std::string> default_value
    );

    bool boolean(
        const std::string& name,
        bool default_value
    );

    //
    // A string of at most max_length characters matching
    // ^[A-Za-z][A-Za-z0-9_]{0,31}$ (a RAPID name); nothing if absent.
    //
    std::optional<std::string> rapid_name(
        const std::string& name,
        bool required
    );

    std::optional<std::string> string(
        const std::string& name,
        bool required,
        std::size_t min_length,
        std::size_t max_length
    );

    //
    // A problem of the whole object (a model validator); only added
    // if the fields themselves were fine, as Pydantic does.
    //
    void model_error(
        const std::string& message
    );

    bool ok() const {
        return errors_.empty();
    }

    void check() const;

private:
    void error(
        const std::string& name,
        const std::string& type,
        const std::string& message,
        const Json& input
    );

    const Json* find(
        const std::string& name
    ) const;

    void missing(
        const std::string& name
    );

    Json values_;
    std::string location_;

    std::vector<Json> errors_;
};

//
// The request body as a JSON object, or 422 (also for a missing
// body). With optional, an empty body or JSON null is an empty
// object, as FastAPI does for a body parameter with a default.
//
Json object_body(
    const Request& request,
    bool optional = false
);

//
// The query parameters as a JSON object of strings.
//
Json query_object(
    const Request& request
);

}  // namespace gw::http
