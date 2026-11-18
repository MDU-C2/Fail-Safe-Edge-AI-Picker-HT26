// Offline differential harness. No camera, HTTP, or robot support is linked.
#include "object_depth.hpp"
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>

namespace {
std::vector<unsigned char> read_bytes(const char* path, std::size_t maximum) {
    std::ifstream input(path, std::ios::binary | std::ios::ate);
    if (!input) throw std::runtime_error("Cannot open fixture");
    const auto length = input.tellg();
    if (length < 0 || static_cast<std::uint64_t>(length) > maximum) throw std::runtime_error("Fixture exceeds limit");
    std::vector<unsigned char> result(static_cast<std::size_t>(length));
    input.seekg(0);
    if (!result.empty() && !input.read(reinterpret_cast<char*>(result.data()), static_cast<std::streamsize>(result.size())))
        throw std::runtime_error("Truncated fixture");
    return result;
}
nlohmann::json read_json(const char* path) {
    const auto bytes = read_bytes(path, 2*1024*1024);
    return nlohmann::json::parse(bytes.begin(), bytes.end());
}
}

int main(int argc, char** argv) {
    try {
        if (argc < 5 || argc > 13) throw std::runtime_error("Usage: object-depth-harness CONFIG DETECTIONS MASKS DEPTH_U16LE [DEPTH_U16LE ...]");
        const auto config = read_json(argv[1]), detections = read_json(argv[2]);
        const int width = detections.at("width").get<int>(), height = detections.at("height").get<int>();
        if (width <= 0 || width > 1920 || height <= 0 || height > 1080) throw std::runtime_error("Invalid fixture dimensions");
        const auto masks = read_bytes(argv[3], 64*1024*1024);
        std::vector<cv::Mat> depths;
        for (int i = 4; i < argc; ++i) {
            const auto bytes = read_bytes(argv[i], 1920*1080*2);
            if (bytes.size() != static_cast<std::size_t>(width)*height*2) throw std::runtime_error("Depth fixture size mismatch");
            cv::Mat depth(height, width, CV_16UC1);
            for (std::size_t j = 0; j < bytes.size()/2; ++j)
                depth.ptr<std::uint16_t>()[j] = std::uint16_t(bytes[2*j]) | (std::uint16_t(bytes[2*j+1]) << 8);
            depths.push_back(std::move(depth));
        }
        std::cout << lynx::describe_objects(detections, masks, depths, config).dump() << '\n';
        return 0;
    } catch (const std::exception& error) {
        std::cerr << "object-depth-harness: " << error.what() << '\n';
        return 1;
    }
}
