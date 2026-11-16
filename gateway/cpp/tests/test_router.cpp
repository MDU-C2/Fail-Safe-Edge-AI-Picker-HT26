// Router: route matching, path parameters, and FastAPI's 404 / 405
// and error bodies.

#include <gtest/gtest.h>

#include "http/router.hpp"

namespace gw::http {
namespace {

Request make_request(
    const std::string& method,
    const std::string& path
) {
    Request request;

    request.method = method;
    request.path = path;

    return request;
}

class RouterTest : public ::testing::Test {
protected:
    void SetUp() override {
        router.get("/api/v1/robot/arms", [](Request&) { return Response::json(Json{{"route", "arms"}}); });

        router.get(
            "/api/v1/robot/arms/{arm}/pose",
            [](Request& request) { return Response::json(Json{{"arm", request.param("arm")}}); }
        );

        router.put(
            "/api/v1/robot/rapid/tasks/{task}/modules/{module}",
            [](Request& request) {
                return Response::json(Json{{"task", request.param("task")}, {"module", request.param("module")}});
            }
        );

        router.post("/api/v1/fails", [](Request&) -> Response { throw HttpError(409, "Busy", {{"Retry-After", "2"}}); });

        router.post("/api/v1/crashes", [](Request&) -> Response { throw std::runtime_error("boom"); });
    }

    Json body(
        const Response& response
    ) {
        return Json::parse(response.body);
    }

    Router router;
};

TEST_F(RouterTest, MatchesLiteralRoutes) {
    auto request = make_request("GET", "/api/v1/robot/arms");

    EXPECT_EQ(body(router.handle(request))["route"], "arms");
}

TEST_F(RouterTest, CapturesPathParameters) {
    auto request = make_request("GET", "/api/v1/robot/arms/left/pose");

    EXPECT_EQ(body(router.handle(request))["arm"], "left");

    auto put = make_request("PUT", "/api/v1/robot/rapid/tasks/T_ROB_L/modules/My%20Module");
    const Json result = body(router.handle(put));

    EXPECT_EQ(result["task"], "T_ROB_L");
    EXPECT_EQ(result["module"], "My Module");
}

TEST_F(RouterTest, UnknownPathIs404) {
    auto request = make_request("GET", "/api/v1/nothing");
    const Response response = router.handle(request);

    EXPECT_EQ(response.status, 404);
    EXPECT_EQ(body(response), (Json{{"detail", "Not Found"}}));
}

TEST_F(RouterTest, WrongMethodIs405) {
    auto request = make_request("DELETE", "/api/v1/robot/arms");
    const Response response = router.handle(request);

    EXPECT_EQ(response.status, 405);
    EXPECT_EQ(body(response), (Json{{"detail", "Method Not Allowed"}}));
}

TEST_F(RouterTest, HttpErrorsBecomeDetailBodies) {
    auto request = make_request("POST", "/api/v1/fails");
    const Response response = router.handle(request);

    EXPECT_EQ(response.status, 409);
    EXPECT_EQ(body(response), (Json{{"detail", "Busy"}}));
    ASSERT_EQ(response.headers.size(), 1u);
    EXPECT_EQ(response.headers[0].first, "Retry-After");
}

TEST_F(RouterTest, OtherExceptionsAre500) {
    auto request = make_request("POST", "/api/v1/crashes");

    EXPECT_EQ(router.handle(request).status, 500);
}

TEST(Request, HeadersIgnoreCase) {
    Request request;

    request.headers = {{"authorization", "Bearer x"}};

    EXPECT_EQ(request.header("Authorization"), "Bearer x");
    EXPECT_FALSE(request.header("X-Dev-Client").has_value());
}

TEST(Request, InvalidJsonBodyIs422) {
    Request request;

    request.body = "{not json";

    try {
        parse_json_body(request);
        FAIL() << "expected a 422";
    } catch (const HttpError& error) {
        EXPECT_EQ(error.status, 422);
        EXPECT_TRUE(error.detail.is_array());
    }
}

TEST(Json, InvalidUtf8IsReplacedNotThrown) {
    EXPECT_NO_THROW(to_text(Json{{"text", std::string("\xff\xfe")}}));
}

}  // namespace
}  // namespace gw::http
