// How controller error responses become messages, as _error_message
// in gateway/rws/client.py.

#include <gtest/gtest.h>

#include "rws/errors.hpp"

namespace gw::rws {
namespace {

TEST(RwsErrors, JsonStatusMessageWithoutSourceLocation) {
    const std::string body =
        R"({"_embedded": {"status": {"code": -1073445865, "msg": )"
        R"("C:\\BUILDAGENTS\\WORK\\rws_resource_rapid.cpp[1602] ERROR: Operation not allowed in current state"}}})";

    EXPECT_EQ(error_message(body, "Bad Request"), "Operation not allowed in current state");
}

TEST(RwsErrors, XhtmlSpanMessage) {
    const std::string body =
        "<html><body><div class=\"status\">"
        "<span class=\"code\">-1</span><span class=\"msg\">Value &quot;[1,2&quot; is not &lt;num&gt;</span>"
        "</div></body></html>";

    EXPECT_EQ(error_message(body, "Bad Request"), "Value \"[1,2\" is not <num>");
}

TEST(RwsErrors, FallsBackToTextThenReason) {
    EXPECT_EQ(error_message("plain failure", "Bad Request"), "plain failure");
    EXPECT_EQ(error_message("", "Not Found"), "Not Found");
    EXPECT_EQ(error_message(std::string(500, 'x'), "x").size(), 300u);
}

TEST(RwsErrors, StripsEverySourceLocation) {
    EXPECT_EQ(
        strip_source_location("D:\\a\\b.cpp[12]: first C:\\c.cpp[3] ERROR:   second"),
        "first second"
    );

    //
    // Not a location: no line number, or no .cpp.
    //
    EXPECT_EQ(strip_source_location("C:\\file.cpp[] x"), "C:\\file.cpp[] x");
    EXPECT_EQ(strip_source_location("C:\\file.txt[1] x"), "C:\\file.txt[1] x");
}

TEST(RwsErrors, HtmlUnescape) {
    EXPECT_EQ(html_unescape("a &amp; b &#65;&#x42; &unknown; &"), "a & b AB &unknown; &");
    EXPECT_EQ(html_unescape("&#228;"), "\xc3\xa4");
}

}  // namespace
}  // namespace gw::rws
