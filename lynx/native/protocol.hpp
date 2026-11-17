#pragma once

#include <array>
#include <cstdint>
#include <cstring>
#include <istream>
#include <ostream>
#include <stdexcept>
#include <string>
#include <vector>

namespace lynx {

constexpr std::uint32_t kProtocolVersion = 1;
constexpr std::uint32_t kMaxWidth = 1920;
constexpr std::uint32_t kMaxHeight = 1080;
constexpr std::uint32_t kMaxJsonBytes = 65536;
constexpr std::uint32_t kMaxPerceptionJsonBytes = 4 * 1024 * 1024;
constexpr std::uint32_t kMaxMaskBytes = 64 * 1024 * 1024;

inline std::uint32_t read_u32(const unsigned char* p) {
    return std::uint32_t(p[0]) | (std::uint32_t(p[1]) << 8) |
           (std::uint32_t(p[2]) << 16) | (std::uint32_t(p[3]) << 24);
}

inline void append_u32(std::vector<unsigned char>& out, std::uint32_t value) {
    for (int shift = 0; shift < 32; shift += 8)
        out.push_back(static_cast<unsigned char>(value >> shift));
}

inline void read_exact(std::istream& input, void* destination, std::size_t length) {
    input.read(static_cast<char*>(destination), static_cast<std::streamsize>(length));
    if (input.gcount() != static_cast<std::streamsize>(length))
        throw std::runtime_error("Truncated native request");
}

struct Request {
    std::uint32_t id = 0;
    std::uint32_t width = 0;
    std::uint32_t height = 0;
    std::vector<unsigned char> bgr;
    bool perception = false;
    std::string config_json;
    std::vector<std::vector<unsigned char>> depth_bytes;
};

// No allocation uses peer-provided lengths before dimensions and framing validate.
inline bool read_request(std::istream& input, Request& request) {
    std::array<unsigned char, 24> header{};
    input.read(reinterpret_cast<char*>(header.data()), header.size());
    if (input.gcount() == 0 && input.eof()) return false;
    if (input.gcount() != static_cast<std::streamsize>(header.size()))
        throw std::runtime_error("Truncated native header");
    request.perception = std::memcmp(header.data(), "LNXP", 4) == 0;
    if (!request.perception && std::memcmp(header.data(), "LNXQ", 4) != 0)
        throw std::runtime_error("Unknown native protocol magic");
    request.id = read_u32(header.data() + 4);
    request.width = read_u32(header.data() + 8);
    request.height = read_u32(header.data() + 12);
    const auto bytes = read_u32(header.data() + 16);
    const auto samples = read_u32(header.data() + 20);
    if (!request.id || (!request.perception && samples != 0))
        throw std::runtime_error("Invalid native request ID or reserved field");
    if (!request.width || !request.height || request.width > kMaxWidth || request.height > kMaxHeight)
        throw std::runtime_error("Native frame exceeds configured limits");
    const auto expected = std::uint64_t(request.width) * request.height * 3;
    if (bytes != expected) throw std::runtime_error("BGR length does not match frame dimensions");
    request.config_json.clear();
    request.depth_bytes.clear();
    if (request.perception) {
        std::array<unsigned char, 8> extension{};
        read_exact(input, extension.data(), extension.size());
        const auto config_size = read_u32(extension.data());
        if (samples < 1 || samples > 9 || !config_size || config_size > kMaxJsonBytes ||
            read_u32(extension.data() + 4) != 0)
            throw std::runtime_error("Invalid perception framing");
        request.config_json.resize(config_size);
        read_exact(input, request.config_json.data(), config_size);
        // Depth is streamed first so the coordinator can release each sample as
        // it is sent, rather than retaining a second full copy of the burst.
        for (std::uint32_t i = 0; i < samples; ++i) {
            request.depth_bytes.emplace_back(std::size_t(request.width) * request.height * 2);
            auto& data = request.depth_bytes.back();
            read_exact(input, data.data(), data.size());
        }
    }
    request.bgr.resize(bytes);
    read_exact(input, request.bgr.data(), request.bgr.size());
    if (request.perception) {
        // Gateway wire format is planar BGR888p. Convert one image in place
        // through a bounded additional buffer, outside the ML runtime.
        std::vector<unsigned char> interleaved(bytes);
        const auto pixels = std::size_t(request.width) * request.height;
        for (std::size_t p = 0; p < pixels; ++p)
            for (int c = 0; c < 3; ++c) interleaved[3*p+c] = request.bgr[c*pixels+p];
        request.bgr.swap(interleaved);
    }
    return true;
}

inline void write_response(std::ostream& output, std::uint32_t id, std::uint32_t status,
                           const std::string& json, const std::vector<unsigned char>& masks = {},
                           std::uint32_t json_limit = kMaxJsonBytes) {
    if (json.size() > json_limit || masks.size() > kMaxMaskBytes)
        throw std::runtime_error("Native response exceeds configured limits");
    std::vector<unsigned char> header{'L', 'N', 'X', 'R'};
    append_u32(header, id);
    append_u32(header, status);
    append_u32(header, static_cast<std::uint32_t>(json.size()));
    append_u32(header, static_cast<std::uint32_t>(masks.size()));
    output.write(reinterpret_cast<const char*>(header.data()), header.size());
    output.write(json.data(), static_cast<std::streamsize>(json.size()));
    if (!masks.empty())
        output.write(reinterpret_cast<const char*>(masks.data()), static_cast<std::streamsize>(masks.size()));
    output.flush();
    if (!output) throw std::runtime_error("Native response pipe is closed");
}

inline std::string json_string(const std::string& text) {
    std::string result = "\"";
    constexpr char hex[] = "0123456789abcdef";
    for (const unsigned char ch : text) {
        if (ch == '"' || ch == '\\') { result += '\\'; result += char(ch); }
        else if (ch < 32) {
            result += "\\u00";
            result += hex[ch >> 4];
            result += hex[ch & 15];
        } else result += char(ch);
    }
    return result + '"';
}

}  // namespace lynx
