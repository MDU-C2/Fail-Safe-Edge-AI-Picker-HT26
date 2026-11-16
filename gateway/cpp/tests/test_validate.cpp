// Request validation with Pydantic v2's lax rules and FastAPI's 422.

#include <gtest/gtest.h>

#include "http/validate.hpp"

namespace gw::http {
namespace {

Json errors_of(
    const Fields& fields
) {
    try {
        fields.check();
    } catch (const HttpError& error) {
        EXPECT_EQ(error.status, 422);

        return error.detail;
    }

    return Json::array();
}

TEST(Validate, NumbersAcceptNumericStringsButNotWords) {
    Fields fields(Json::parse(R"({"a": 1, "b": 2.5, "c": "3", "d": "high", "e": null, "f": [1], "g": true})"), "body");

    EXPECT_EQ(fields.number("a", true), 1.0);
    EXPECT_EQ(fields.number("b", true), 2.5);
    EXPECT_EQ(fields.number("c", true), 3.0);
    EXPECT_EQ(fields.number("d", true), std::nullopt);
    EXPECT_EQ(fields.number("e", false), std::nullopt);
    EXPECT_EQ(fields.number("f", true), std::nullopt);
    EXPECT_EQ(fields.number("g", true), 1.0);
    EXPECT_EQ(fields.number("missing", false), std::nullopt);
    EXPECT_EQ(fields.number("absent", true), std::nullopt);

    const Json errors = errors_of(fields);

    ASSERT_EQ(errors.size(), 3u);
    EXPECT_EQ(errors[0]["type"], "float_parsing");
    EXPECT_EQ(errors[0]["loc"], Json::array({"body", "d"}));
    EXPECT_EQ(errors[1]["type"], "float_type");
    EXPECT_EQ(errors[2]["type"], "missing");
}

TEST(Validate, NumbersMustBeFiniteAndInRange) {
    Fields fields(Json::parse(R"({"speed": 0, "fast": 2000, "ok": 50, "force": 0, "inf": "inf"})"), "body");

    EXPECT_EQ(fields.number("speed", false, {1.0, std::nullopt, 1500.0}), std::nullopt);
    EXPECT_EQ(fields.number("fast", false, {1.0, std::nullopt, 1500.0}), std::nullopt);
    EXPECT_EQ(fields.number("ok", false, {1.0, std::nullopt, 1500.0}), 50.0);
    EXPECT_EQ(fields.number("force", false, {std::nullopt, 0.0, 20.0}), std::nullopt);
    EXPECT_EQ(fields.number("inf", false), std::nullopt);

    const Json errors = errors_of(fields);

    ASSERT_EQ(errors.size(), 4u);
    EXPECT_EQ(errors[0]["msg"], "Input should be greater than or equal to 1");
    EXPECT_EQ(errors[1]["msg"], "Input should be less than or equal to 1500");
    EXPECT_EQ(errors[2]["msg"], "Input should be greater than 0");
    EXPECT_EQ(errors[3]["type"], "finite_number");
}

TEST(Validate, ChoicesAreLiterals) {
    Fields fields(Json::parse(R"({"arm": "middle", "action": "grip"})"), "body");

    EXPECT_EQ(fields.choice("arm", {"left", "right"}, "left"), std::nullopt);
    EXPECT_EQ(fields.choice("action", {"grip", "open", "move"}, std::nullopt), "grip");
    EXPECT_EQ(fields.choice("absent", {"left", "right"}, "left"), "left");

    const Json errors = errors_of(fields);

    ASSERT_EQ(errors.size(), 1u);
    EXPECT_EQ(errors[0]["msg"], "Input should be 'left' or 'right'");

    Fields three(Json::parse(R"({"c": "World"})"), "query");

    three.choice("c", {"Base", "Wobj", "Tool"}, "");

    EXPECT_EQ(errors_of(three)[0]["msg"], "Input should be 'Base', 'Wobj' or 'Tool'");
    EXPECT_EQ(errors_of(three)[0]["loc"], Json::array({"query", "c"}));
}

TEST(Validate, BooleansAreLax) {
    Fields fields(Json::parse(R"({"a": true, "b": 0, "c": "yes", "d": "Off", "e": "maybe", "f": 2})"), "body");

    EXPECT_TRUE(fields.boolean("a", false));
    EXPECT_FALSE(fields.boolean("b", true));
    EXPECT_TRUE(fields.boolean("c", false));
    EXPECT_FALSE(fields.boolean("d", true));
    fields.boolean("e", true);
    fields.boolean("f", true);
    EXPECT_TRUE(fields.boolean("absent", true));

    EXPECT_EQ(errors_of(fields).size(), 2u);
}

TEST(Validate, RapidNamesAndStrings) {
    Fields fields(
        Json::parse(R"({"ok": "tool0", "dots": "../tool0", "long": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "digit": "1Module", "empty": ""})"),
        "query"
    );

    EXPECT_EQ(fields.rapid_name("ok", false), "tool0");
    EXPECT_EQ(fields.rapid_name("dots", false), std::nullopt);
    EXPECT_EQ(fields.rapid_name("long", false), std::nullopt);
    EXPECT_EQ(fields.rapid_name("digit", false), std::nullopt);
    EXPECT_EQ(fields.rapid_name("absent", false), std::nullopt);

    EXPECT_EQ(errors_of(fields).size(), 3u);

    Fields strings(Json::parse("{\"empty\": \"\", \"snowmen\": \"\xe2\x98\x83\xe2\x98\x83\", \"number\": 5}"), "body");

    EXPECT_EQ(strings.string("empty", true, 1, 10), std::nullopt);
    EXPECT_EQ(strings.string("snowmen", true, 1, 2), "\xe2\x98\x83\xe2\x98\x83");
    EXPECT_EQ(strings.string("number", true, 1, 10), std::nullopt);

    const Json errors = errors_of(strings);

    ASSERT_EQ(errors.size(), 2u);
    EXPECT_EQ(errors[0]["type"], "string_too_short");
    EXPECT_EQ(errors[1]["type"], "string_type");
}

TEST(Validate, ModelErrorsOnlyWhenTheFieldsAreFine) {
    Fields bad(Json::parse(R"({"x": "high"})"), "body");

    bad.number("x", true);
    bad.model_error("Give all of rx, ry and rz, or none of them");

    EXPECT_EQ(errors_of(bad).size(), 1u);
    EXPECT_EQ(errors_of(bad)[0]["type"], "float_parsing");

    Fields good(Json::parse(R"({"x": 1})"), "body");

    good.number("x", true);
    good.model_error("Give all of rx, ry and rz, or none of them");

    EXPECT_EQ(errors_of(good)[0]["msg"], "Value error, Give all of rx, ry and rz, or none of them");
    EXPECT_EQ(errors_of(good)[0]["loc"], Json::array({"body"}));
}

TEST(Validate, ObjectBodies) {
    Request request;

    request.body = "";
    EXPECT_THROW(object_body(request), HttpError);
    EXPECT_EQ(object_body(request, true), Json::object());

    request.body = "null";
    EXPECT_EQ(object_body(request, true), Json::object());

    request.body = "[1, 2]";
    EXPECT_THROW(object_body(request, true), HttpError);

    request.body = R"({"arm": "right"})";
    EXPECT_EQ(object_body(request)["arm"], "right");
}

}  // namespace
}  // namespace gw::http
