#include "http/router.hpp"

#include "util/log.hpp"
#include "util/text.hpp"

namespace gw::http {

// ================================================================
// Messages
// ================================================================

std::string to_text(
    const Json& value
) {
    return value.dump(
        -1,
        ' ',
        false,
        nlohmann::detail::error_handler_t::replace
    );
}

std::optional<std::string_view> Request::header(
    std::string_view name
) const {
    for (const auto& [key, value] : headers) {
        if (iequals(key, name)) {
            return std::string_view(value);
        }
    }

    return std::nullopt;
}

std::optional<std::string> Request::query_value(
    const std::string& name
) const {
    if (const auto it = query.find(name); it != query.end()) {
        return it->second;
    }

    return std::nullopt;
}

const std::string& Request::param(
    const std::string& name
) const {
    return params.at(name);
}

Response Response::json(
    const Json& value,
    int status
) {
    Response response;

    response.status = status;
    response.body = to_text(value);

    return response;
}

std::size_t Response::body_size() const {
    return shared_body ? shared_body.size : body.size();
}

const void* Response::body_data() const {
    return shared_body ? static_cast<const void*>(shared_body.data) : body.data();
}

HttpError::HttpError(
    int status_code,
    Json detail_value,
    Headers extra_headers
)
    : status(status_code),
      detail(std::move(detail_value)),
      headers(std::move(extra_headers)),
      message_(detail.is_string() ? detail.get<std::string>() : to_text(detail)) {}

const char* HttpError::what() const noexcept {
    return message_.c_str();
}

Response error_response(
    int status,
    const Json& detail,
    const Headers& headers
) {
    Response response = Response::json(Json{{"detail", detail}}, status);

    response.headers = headers;

    return response;
}

HttpError validation_error(
    std::vector<Json> location,
    std::string message,
    std::string type,
    Json input
) {
    Json item = {
        {"type", std::move(type)},
        {"loc", std::move(location)},
        {"msg", std::move(message)},
        {"input", std::move(input)},
    };

    return HttpError(422, Json::array({item}));
}

Json parse_json_body(
    const Request& request
) {
    Json body = Json::parse(request.body, nullptr, false);

    if (body.is_discarded()) {
        throw validation_error(
            {"body"},
            "JSON decode error",
            "json_invalid"
        );
    }

    return body;
}

// ================================================================
// Routing
// ================================================================

std::vector<std::string> split_path(
    const std::string& path
) {
    std::vector<std::string> segments;

    std::size_t start = 0;

    while (start < path.size()) {
        if (path[start] == '/') {
            ++start;
            continue;
        }

        auto end = path.find('/', start);

        if (end == std::string::npos) {
            end = path.size();
        }

        segments.push_back(url_decode(std::string_view(path).substr(start, end - start), false));

        start = end;
    }

    return segments;
}

void Router::add(
    std::string method,
    std::string pattern,
    Handler handler
) {
    routes_.push_back(
        Route{
            std::move(method),
            split_path(pattern),
            std::move(handler),
        }
    );
}

void Router::get(
    std::string pattern,
    Handler handler
) {
    add("GET", std::move(pattern), std::move(handler));
}

void Router::post(
    std::string pattern,
    Handler handler
) {
    add("POST", std::move(pattern), std::move(handler));
}

void Router::put(
    std::string pattern,
    Handler handler
) {
    add("PUT", std::move(pattern), std::move(handler));
}

void Router::del(
    std::string pattern,
    Handler handler
) {
    add("DELETE", std::move(pattern), std::move(handler));
}

void Router::websocket(
    std::string pattern,
    WebSocketAuthorizer authorize,
    WebSocketHandler open
) {
    websocket_routes_.push_back(
        WebSocketRoute{
            split_path(pattern),
            std::move(authorize),
            std::move(open),
        }
    );
}

const WebSocketRoute* Router::find_websocket(
    Request& request
) const {
    const auto segments = split_path(request.path);

    for (const WebSocketRoute& route : websocket_routes_) {
        std::map<std::string, std::string> params;

        if (match_parts(route.parts, segments, params)) {
            request.params = std::move(params);

            return &route;
        }
    }

    return nullptr;
}

bool Router::match(
    const Route& route,
    const std::vector<std::string>& segments,
    std::map<std::string, std::string>& params
) {
    return match_parts(route.parts, segments, params);
}

bool Router::match_parts(
    const std::vector<std::string>& parts,
    const std::vector<std::string>& segments,
    std::map<std::string, std::string>& params
) {
    if (parts.size() != segments.size()) {
        return false;
    }

    params.clear();

    for (std::size_t i = 0; i < segments.size(); ++i) {
        const std::string& part = parts[i];

        if (part.size() > 2 && part.front() == '{' && part.back() == '}') {
            if (segments[i].empty()) {
                return false;
            }

            params[part.substr(1, part.size() - 2)] = segments[i];
        } else if (part != segments[i]) {
            return false;
        }
    }

    return true;
}

Response Router::handle(
    Request& request
) const {
    const auto segments = split_path(request.path);

    std::string allowed;

    for (const Route& route : routes_) {
        std::map<std::string, std::string> params;

        if (!match(route, segments, params)) {
            continue;
        }

        if (route.method != request.method) {
            allowed += allowed.empty() ? "" : ", ";
            allowed += route.method;
            continue;
        }

        request.params = std::move(params);

        try {
            return route.handler(request);

        } catch (const HttpError& error) {
            return error_response(error.status, error.detail, error.headers);

        } catch (const std::exception& error) {
            log::error(format("%s %s failed: %s", request.method.c_str(), request.path.c_str(), error.what()));

            return error_response(500, "Internal Server Error");
        }
    }

    if (!allowed.empty()) {
        return error_response(405, "Method Not Allowed", {{"Allow", allowed}});
    }

    return error_response(404, "Not Found");
}

}  // namespace gw::http
