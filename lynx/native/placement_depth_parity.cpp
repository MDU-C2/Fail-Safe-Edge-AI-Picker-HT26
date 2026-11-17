// Offline differential-test executable. Never linked into the robot worker.
#ifdef LYNX_PLACEMENT_DEPTH_PARITY_MAIN
#include "placement_depth.hpp"
#include <fstream>
#include <iostream>
#include <stdexcept>
#include <string>

using namespace lynx::placement;

namespace {
json shared_json(const std::vector<SharedRegion>& shared) {
    json output = json::array();
    for (const auto& region : shared) {
        json coordinates = json::array();
        for (auto point : region.pixels) coordinates.push_back({point.y, point.x});
        output.push_back({{"owners", region.owners}, {"pixels", coordinates}});
    }
    return output;
}
void read_matrix(const std::string& path, cv::Mat& matrix) {
    std::ifstream input(path, std::ios::binary);
    const auto bytes = static_cast<std::streamsize>(matrix.total() * matrix.elemSize());
    if (!input.read(reinterpret_cast<char*>(matrix.data), bytes))
        throw std::runtime_error("Truncated test matrix");
}
json run(const json& request) {
    const int height = request.at("height"), width = request.at("width");
    if (height <= 0 || width <= 0 || height > 4096 || width > 4096 ||
        std::int64_t(height) * width > 8388608)
        throw std::runtime_error("Unsupported test frame dimensions");
    const std::string dtype = request.value("depth_dtype", "float64");
    if (dtype != "uint16" && dtype != "float32" && dtype != "float64")
        throw std::runtime_error("Unsupported test depth type");
    cv::Mat depth(height, width, dtype == "uint16" ? CV_16U : dtype == "float32" ? CV_32F : CV_64F);
    read_matrix(request.at("depth_path"), depth);
    cv::Matx33d matrix;
    cv::Vec3d origin;
    for (int y = 0; y < 3; ++y) {
        origin[y] = request.at("translation").at(y);
        for (int x = 0; x < 3; ++x) matrix(y, x) = request.at("transform").at(y).at(x);
    }
    const double cell_size = request.value("cell_size", 80.);
    const double table_z = request.value("table_z", 0.);
    const double check_height = request.value("check_height", 160.);
    if (request.at("mode") == "regions") {
        cv::Mat owners(height, width, CV_32S);
        read_matrix(request.at("owners_path"), owners);
        const cv::Mat original = owners.clone();
        const bool consume = request.value("consume_owners", false);
        const auto result = assign_depth_regions(depth, request.at("objects"), request.at("locations"),
            owners, request.at("assignments"), matrix, origin, request.at("intrinsics"),
            cell_size, table_z, check_height, consume);
        std::ofstream output(request.at("output_path").get<std::string>(), std::ios::binary);
        if (!output.write(reinterpret_cast<const char*>(result.owners.data),
                          static_cast<std::streamsize>(result.owners.total() * result.owners.elemSize())))
            throw std::runtime_error("Cannot save test ownership matrix");
        return {{"assignments", result.assignments}, {"shared", shared_json(result.shared)},
                {"input_unchanged", cv::countNonZero(owners != original) == 0},
                {"aliases_input", result.owners.data == owners.data}};
    }
    if (request.at("mode") != "uncertainty") throw std::runtime_error("Unknown test mode");
    const auto result = cup_uncertainty_regions(depth, request.at("objects"), request.at("locations"),
        request.at("assignments"), matrix, origin, request.at("intrinsics"), cell_size, table_z,
        check_height, request.value("radius_mm", 40.), request.value("tail_radius_mm", 80.));
    return {{"shared", shared_json(result.first)}, {"evidence", result.second}};
}
}
int main() {
    std::string line;
    while (std::getline(std::cin, line)) {
        try {
            std::cout << run(json::parse(line)).dump() << std::endl;
        } catch (const std::exception& error) {
            std::cout << json({{"error", error.what()}}).dump() << std::endl;
        }
    }
}
#endif
