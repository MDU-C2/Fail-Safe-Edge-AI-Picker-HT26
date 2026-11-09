// SHA-256 against Python's hashlib, time formatting, text helpers,
// and the mock camera's test pattern.

#include <gtest/gtest.h>

#include "camera/mock_producer.hpp"
#include "util/sha256.hpp"
#include "util/text.hpp"
#include "util/time.hpp"

namespace gw {
namespace {

TEST(Sha256, MatchesHashlib) {
    //
    // Values from hashlib.sha256(s.encode()).hexdigest(); the
    // lengths cover the one- and two-block paddings.
    //
    EXPECT_EQ(sha256_hex(""), "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855");
    EXPECT_EQ(sha256_hex("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    EXPECT_EQ(sha256_hex("test-token-admin"), "e211d8dc92775d53e4be89b8f2b0481a4bf64016e50e74113a33ea897d0e05ea");
    EXPECT_EQ(sha256_hex(std::string(55, 'a')), "9f4390f8d30c2dd92ec9f095b65e2b9ae9b0a925a5258e241c9f1e910f734318");
    EXPECT_EQ(sha256_hex(std::string(56, 'a')), "b35439a4ac6f0948b6d6f9e3c6af0f5f590ce20f1bde7090ef7970686ec6738a");
    EXPECT_EQ(sha256_hex(std::string(64, 'a')), "ffe054fe7ae0cb6dc65c3af9b61d5209f439851db43d0ba5997337df154668eb");
    EXPECT_EQ(sha256_hex(std::string(1000, 'a')), "41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3");
}

TEST(Sha256, ConstantTimeEqual) {
    EXPECT_TRUE(constant_time_equal("abc", "abc"));
    EXPECT_FALSE(constant_time_equal("abc", "abd"));
    EXPECT_FALSE(constant_time_equal("abc", "abcd"));
}

TEST(Time, FormatsLikePydanticAndIsoformat) {
    const WallTime whole{std::chrono::seconds(1'767'225'600)};
    const WallTime fraction = whole + std::chrono::microseconds(123'456);

    EXPECT_EQ(format_json_time(whole), "2026-01-01T00:00:00.000000Z");
    EXPECT_EQ(format_json_time(fraction), "2026-01-01T00:00:00.123456Z");

    EXPECT_EQ(format_iso_time(whole), "2026-01-01T00:00:00+00:00");
    EXPECT_EQ(format_iso_time(fraction), "2026-01-01T00:00:00.123456+00:00");
}

TEST(Text, Numbers) {
    EXPECT_EQ(parse_double(" 2.5 "), 2.5);
    EXPECT_FALSE(parse_double("2.5x").has_value());
    EXPECT_FALSE(parse_double("").has_value());
    EXPECT_EQ(parse_integer("5001"), 5001);
    EXPECT_FALSE(parse_integer("50.1").has_value());
    EXPECT_EQ(format_g(5.0), "5");
    EXPECT_EQ(format_g(2.5), "2.5");
}

TEST(Text, UrlCoding) {
    EXPECT_EQ(url_decode("a%20b+c", true), "a b c");
    EXPECT_EQ(url_decode("a%20b+c", false), "a b+c");
    EXPECT_EQ(url_decode("100%", false), "100%");
    EXPECT_EQ(url_encode("Default User/x"), "Default%20User%2Fx");
}

TEST(MockCamera, PatternMatchesThePythonMock) {
    const auto rgb = MockCameraProducer::make_rgb();
    const auto depth = MockCameraProducer::make_depth();

    constexpr int W = MockCameraProducer::WIDTH;
    constexpr int H = MockCameraProducer::HEIGHT;

    ASSERT_EQ(rgb.size(), std::size_t(3 * W * H));
    ASSERT_EQ(depth.size(), std::size_t(2 * W * H));

    //
    // Blue grows downwards, green to the right, red is 128.
    //
    EXPECT_EQ(rgb[0], 0);
    EXPECT_EQ(rgb[(H - 1) * W], 255);
    EXPECT_EQ(rgb[W * H + W - 1], 255);
    EXPECT_EQ(rgb[2 * W * H], 128);

    auto depth_at = [&](int row, int column) {
        const std::size_t i = 2 * (std::size_t(row) * W + column);

        return depth[i] | (depth[i + 1] << 8);
    };

    EXPECT_EQ(depth_at(0, 0), 0);
    EXPECT_EQ(depth_at(0, 1), 500);
    EXPECT_EQ(depth_at(H - 1, W - 1), 500 + H - 1);
}

}  // namespace
}  // namespace gw
