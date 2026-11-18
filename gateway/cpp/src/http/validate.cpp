#include "http/validate.hpp"

#include <cmath>
#include <cstdint>

#include "util/text.hpp"

namespace gw::http {

Fields::Fields(
    Json values,
    std::string location
)
    : values_(std::move(values)),
      location_(Json::array({std::move(location)})) {}

std::optional<Json> Fields::list(
    const std::string& name,
    bool required,
    std::size_t min_length,
    std::size_t max_length
) {
    const Json* value = find(name);

    if (value == nullptr || (value->is_null() && !required)) {
        if (required) {
            missing(name);
        }

        return std::nullopt;
    }

    if (!value->is_array()) {
        error(name, "list_type", "Input should be a valid list", *value);
        return std::nullopt;
    }

    const std::size_t size = value->size();

    if (size < min_length) {
        error(
            name,
            "too_short",
            format("List should have at least %zu item%s after validation, not %zu", min_length, min_length == 1 ? "" : "s", size),
            *value
        );

        return std::nullopt;
    }

    if (size > max_length) {
        error(
            name,
            "too_long",
            format("List should have at most %zu item%s after validation, not %zu", max_length, max_length == 1 ? "" : "s", size),
            *value
        );

        return std::nullopt;
    }

    return *value;
}

Fields Fields::item(
    const std::string& name,
    std::size_t index,
    const std::string& model
) const {
    const Json* list = find(name);
    const Json value = list != nullptr && list->is_array() && index < list->size() ? (*list)[index] : Json();

    Fields fields(value, "");

    fields.location_ = location_;
    fields.location_.push_back(name);
    fields.location_.push_back(index);

    if (!value.is_object()) {
        fields.error("", "model_type", "Input should be a valid dictionary or instance of " + model, value);
    }

    return fields;
}

Fields Fields::object(
    const std::string& name,
    const std::string& model,
    bool required
) {
    const Json* found = find(name);
    const bool absent = found == nullptr || found->is_null();

    //
    // Missing and required: that one error, none for its fields
    // (they see no object, as for a non-object item()).
    //
    if (absent && required) {
        missing(name);
    }

    const Json value = !absent ? *found : required ? Json() : Json::object();

    Fields fields(value, "");

    fields.location_ = location_;
    fields.location_.push_back(name);

    if (!absent && !value.is_object()) {
        fields.error("", "model_type", "Input should be a valid dictionary or instance of " + model, value);
    }

    return fields;
}

void Fields::merge(
    const Fields& other
) {
    errors_.insert(errors_.end(), other.errors_.begin(), other.errors_.end());
}

void Fields::error(
    const std::string& name,
    const std::string& type,
    const std::string& message,
    const Json& input
) {
    Json location = location_;

    if (!name.empty()) {
        location.push_back(name);
    }

    errors_.push_back(
        Json{
            {"type", type},
            {"loc", location},
            {"msg", message},
            {"input", input},
        }
    );
}

const Json* Fields::find(
    const std::string& name
) const {
    if (!values_.is_object()) {
        return nullptr;
    }

    const auto it = values_.find(name);

    return it == values_.end() ? nullptr : &*it;
}

void Fields::missing(
    const std::string& name
) {
    //
    // An item that is no object is one model_type error (item()),
    // not one per field, as in Pydantic.
    //
    if (!values_.is_object()) {
        return;
    }

    error(name, "missing", "Field required", values_);
}

std::optional<double> Fields::number(
    const std::string& name,
    bool required,
    Range range
) {
    const Json* value = find(name);

    if (value == nullptr || (value->is_null() && !required)) {
        if (required) {
            missing(name);
        }

        return std::nullopt;
    }

    std::optional<double> number;

    if (value->is_number()) {
        number = value->get<double>();
    } else if (value->is_boolean()) {
        number = value->get<bool>() ? 1.0 : 0.0;
    } else if (value->is_string()) {
        number = parse_double(value->get<std::string>());

        if (!number) {
            error(name, "float_parsing", "Input should be a valid number, unable to parse string as a number", *value);
            return std::nullopt;
        }
    } else {
        error(name, "float_type", "Input should be a valid number", *value);
        return std::nullopt;
    }

    if (!std::isfinite(*number)) {
        error(name, "finite_number", "Input should be a finite number", *value);
        return std::nullopt;
    }

    if (range.ge && !(*number >= *range.ge)) {
        error(name, "greater_than_equal", "Input should be greater than or equal to " + format_g(*range.ge), *value);
        return std::nullopt;
    }

    if (range.gt && !(*number > *range.gt)) {
        error(name, "greater_than", "Input should be greater than " + format_g(*range.gt), *value);
        return std::nullopt;
    }

    if (range.le && !(*number <= *range.le)) {
        error(name, "less_than_equal", "Input should be less than or equal to " + format_g(*range.le), *value);
        return std::nullopt;
    }

    return number;
}

std::optional<std::string> Fields::choice(
    const std::string& name,
    std::initializer_list<const char*> options,
    std::optional<std::string> default_value
) {
    const Json* value = find(name);

    if (value == nullptr) {
        if (!default_value) {
            missing(name);
        }

        return default_value;
    }

    if (value->is_string()) {
        for (const char* option : options) {
            if (value->get<std::string>() == option) {
                return std::string(option);
            }
        }
    }

    //
    // "Input should be 'left' or 'right'"
    //
    std::string expected;
    std::size_t i = 0;

    for (const char* option : options) {
        if (i > 0) {
            expected += i + 1 == options.size() ? " or " : ", ";
        }

        expected += format("'%s'", option);
        ++i;
    }

    error(name, "literal_error", "Input should be " + expected, *value);

    return std::nullopt;
}

bool Fields::boolean(
    const std::string& name,
    bool default_value
) {
    const Json* value = find(name);

    if (value == nullptr) {
        return default_value;
    }

    if (value->is_boolean()) {
        return value->get<bool>();
    }

    if (value->is_number_integer() || value->is_number_float()) {
        const double number = value->get<double>();

        if (number == 0.0 || number == 1.0) {
            return number == 1.0;
        }
    }

    if (value->is_string()) {
        const std::string text = to_lower(trim(value->get<std::string>()));

        for (const char* yes : {"1", "on", "t", "true", "y", "yes"}) {
            if (text == yes) {
                return true;
            }
        }

        for (const char* no : {"0", "off", "f", "false", "n", "no"}) {
            if (text == no) {
                return false;
            }
        }
    }

    error(name, "bool_parsing", "Input should be a valid boolean, unable to interpret input", *value);

    return default_value;
}

std::optional<std::string> Fields::string(
    const std::string& name,
    bool required,
    std::size_t min_length,
    std::size_t max_length
) {
    const Json* value = find(name);

    if (value == nullptr || (value->is_null() && !required)) {
        if (required) {
            missing(name);
        }

        return std::nullopt;
    }

    if (!value->is_string()) {
        error(name, "string_type", "Input should be a valid string", *value);
        return std::nullopt;
    }

    const std::string text = value->get<std::string>();

    //
    // Characters, not bytes, as Python's len().
    //
    std::size_t length = 0;

    for (const char c : text) {
        length += (static_cast<unsigned char>(c) & 0xc0) != 0x80 ? 1 : 0;
    }

    if (length < min_length) {
        error(name, "string_too_short", format("String should have at least %zu character%s", min_length, min_length == 1 ? "" : "s"), *value);
        return std::nullopt;
    }

    if (length > max_length) {
        error(name, "string_too_long", format("String should have at most %zu characters", max_length), *value);
        return std::nullopt;
    }

    return text;
}

std::optional<std::string> Fields::rapid_name(
    const std::string& name,
    bool required
) {
    const auto text = string(name, required, 0, SIZE_MAX);

    if (!text) {
        return std::nullopt;
    }

    bool valid = !text->empty() && text->size() <= 32;

    for (std::size_t i = 0; valid && i < text->size(); ++i) {
        const char c = (*text)[i];
        const bool letter = (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
        const bool digit = c >= '0' && c <= '9';

        valid = i == 0 ? letter : (letter || digit || c == '_');
    }

    if (!valid) {
        error(
            name,
            "string_pattern_mismatch",
            "String should match pattern '^[A-Za-z][A-Za-z0-9_]{0,31}$'",
            *find(name)
        );

        return std::nullopt;
    }

    return text;
}

void Fields::model_error(
    const std::string& message
) {
    if (ok()) {
        error("", "value_error", "Value error, " + message, values_);
    }
}

void Fields::check() const {
    if (!errors_.empty()) {
        throw HttpError(422, Json(errors_));
    }
}

Json object_body(
    const Request& request,
    bool optional
) {
    if (trim(request.body).empty()) {
        if (optional) {
            return Json::object();
        }

        throw validation_error({"body"}, "Field required", "missing");
    }

    Json body = parse_json_body(request);

    if (body.is_null() && optional) {
        return Json::object();
    }

    if (!body.is_object()) {
        throw validation_error(
            {"body"},
            "Input should be a valid dictionary or object to extract fields from",
            "model_attributes_type",
            body
        );
    }

    return body;
}

Json query_object(
    const Request& request
) {
    Json values = Json::object();

    for (const auto& [name, value] : request.query) {
        values[name] = value;
    }

    return values;
}

}  // namespace gw::http
