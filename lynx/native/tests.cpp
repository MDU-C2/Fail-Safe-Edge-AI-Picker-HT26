#include "geometry.hpp"
#include "protocol.hpp"

#include <functional>
#include <iostream>
#include <limits>
#include <sstream>

namespace {

void check(bool passed, const char* message) {
    if (!passed) throw std::runtime_error(message);
}

void rejects(const std::function<void()>& action, const char* message) {
    try { action(); } catch (const std::exception&) { return; }
    throw std::runtime_error(message);
}

std::string request_bytes(std::uint32_t id, std::uint32_t width, std::uint32_t height,
                          std::uint32_t size, std::uint32_t reserved = 0) {
    std::vector<unsigned char> bytes{'L', 'N', 'X', 'Q'};
    lynx::append_u32(bytes, id);
    lynx::append_u32(bytes, width);
    lynx::append_u32(bytes, height);
    lynx::append_u32(bytes, size);
    lynx::append_u32(bytes, reserved);
    return {bytes.begin(), bytes.end()};
}

void test_protocol() {
    lynx::Request request;
    std::istringstream empty;
    check(!lynx::read_request(empty, request), "Clean EOF must exit");
    std::string payload = request_bytes(0x12345678, 2, 1, 6) + std::string("abcdef");
    std::istringstream valid(payload + payload);
    check(lynx::read_request(valid, request) && request.id == 0x12345678 && request.width == 2 &&
          request.height == 1 && request.bgr.size() == 6 && request.bgr[5] == 'f', "BGR request must round-trip");
    check(lynx::read_request(valid, request), "Consecutive requests must not lose framing");
    check(!lynx::read_request(valid, request), "Boundary EOF must exit");
    for (const auto& invalid : {
             request_bytes(0, 2, 1, 6), request_bytes(1, 2, 1, 6, 1),
             request_bytes(1, 0, 1, 0), request_bytes(1, 1921, 1, 5763),
             request_bytes(1, 1, 1081, 3243), request_bytes(1, 2, 1, 0xffffffff),
             request_bytes(1, 2, 1, 6) + "short", std::string("LNXQ")}) {
        rejects([&] { std::istringstream stream(invalid); lynx::read_request(stream, request); },
                "Malformed or unbounded input must be rejected before allocation");
    }
    std::ostringstream response;
    lynx::write_response(response, 17, 0, "{}", {0, 1, 1});
    const auto wire = response.str();
    check(wire.size() == 25 && wire.substr(0, 4) == "LNXR", "Response header length/magic");
    const auto* header = reinterpret_cast<const unsigned char*>(wire.data());
    check(lynx::read_u32(header + 4) == 17 && lynx::read_u32(header + 12) == 2 &&
          lynx::read_u32(header + 16) == 3, "Response fields must be little-endian");
    check(lynx::json_string("\"\\\n") == "\"\\\"\\\\\\u000a\"", "JSON error escaping");
}

void test_letterbox_and_boxes() {
    const auto g = lynx::letterbox_geometry(960, 540, 320);
    check(g.resized_width == 320 && g.resized_height == 180 && g.left == 0 && g.top == 70,
          "16:9 image letterbox");
    lynx::Detection detection;
    detection.box = {0, 70, 320, 250};
    const auto box = lynx::original_box(detection, g);
    check(box == std::array<float, 4>{0, 0, 960, 540}, "Box must map back onto original depth pixels");
    const auto portrait = lynx::letterbox_geometry(481, 640, 320);
    check(portrait.resized_width == 240 && portrait.resized_height == 320 && portrait.left == 40,
          "Letterbox rounding must agree with Python ties-to-even");
    check(lynx::python_round(2.5) == 2 && lynx::python_round(3.5) == 4, "Ties-to-even rounding");
    rejects([] { lynx::letterbox_geometry(640, 0, 320); }, "Reject empty image");
    rejects([] { lynx::letterbox_geometry(640, 360, 321); }, "Reject non-stride model input");
}

void test_nms() {
    lynx::Detection cup, base, duplicate, handle;
    cup.box = base.box = duplicate.box = handle.box = {0, 0, 20, 20};
    cup.label = 0; cup.score = .9f;
    base.label = 1; base.score = .8f;
    handle.label = 2; handle.score = .7f;
    duplicate.label = 0; duplicate.score = .6f;
    const auto kept = lynx::class_aware_nms({duplicate, base, cup, handle}, .7f, 32);
    check(kept.size() == 3 && kept[0].label == 0 && kept[1].label == 1 && kept[2].label == 2,
          "NMS must preserve overlapping CUP, CUP BASE and CUP HANDLE");
    check(lynx::class_aware_nms({cup, base, handle}, .7f, 2).size() == 2, "NMS detection bound");
}

void test_masks() {
    // Prototype outer rows simulate letterbox padding. They must not leak into
    // the original mask, even when their logits are much larger than the object.
    const std::vector<float> logits = {
        100, 100, 100, 100,
        -1, -1, 1, 1,
        -1, -1, 1, 1,
        100, 100, 100, 100,
    };
    const auto mask = lynx::original_mask(logits, 4, 4, {0, 0, 8, 4}, 8, 4);
    check(mask.values == std::vector<unsigned char>({0, 0, 0, 0, 1, 1, 1, 1,
                                                    0, 0, 0, 0, 1, 1, 1, 1,
                                                    0, 0, 0, 0, 1, 1, 1, 1,
                                                    0, 0, 0, 0, 1, 1, 1, 1}),
          "Native retina mask must remove letterbox padding before bilinear sampling");
    const auto cropped = lynx::original_mask(logits, 4, 4, {3.2f, .2f, 6.2f, 2.2f}, 8, 4);
    check(cropped.x == 4 && cropped.y == 1 && cropped.width == 3 && cropped.height == 2 &&
          cropped.values == std::vector<unsigned char>(6, 1), "Fractional box crop uses pixel coordinate comparisons");
    const auto interpolation = lynx::original_mask({-1, 9, -1, 9}, 2, 2, {0, 0, 4, 4}, 4, 4);
    check(interpolation.values[1] == 1,
          "Interpolate logits before threshold: interpolating binary masks would lose this positive pixel");
    const auto border = lynx::original_mask({10, -10, 10, -10}, 2, 2, {0, 0, 4, 4}, 4, 4);
    check(border.values[0] == 1 && border.values[3] == 0, "Bilinear edge coordinates must clamp before computing weights");
    rejects([] {
        lynx::original_mask({std::numeric_limits<float>::quiet_NaN()}, 1, 1, {0, 0, 1, 1}, 1, 1);
    }, "Nonfinite masks must fail closed");
    rejects([] { lynx::original_mask({1}, 2, 2, {0, 0, 1, 1}, 1, 1); }, "Wrong mask shape must fail closed");
}

}  // namespace

int main() {
    try {
        test_protocol();
        test_letterbox_and_boxes();
        test_nms();
        test_masks();
        std::cout << "Native protocol, letterbox, NMS and mask tests passed\n";
        return 0;
    } catch (const std::exception& error) {
        std::cerr << error.what() << '\n';
        return 1;
    }
}
