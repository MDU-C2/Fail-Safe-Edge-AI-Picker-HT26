// Calibration store: the file format shared with the Python gateway,
// atomic writes, invalid files, and the json.dumps size measure.

#include <gtest/gtest.h>

#include <unistd.h>

#include <filesystem>
#include <fstream>
#include <sstream>

#include "calibration/service.hpp"

namespace gw {
namespace {

namespace fs = std::filesystem;

using http::Json;

class FixedClock : public Clock {
public:
    WallTime wall() const override {
        return WallTime(std::chrono::seconds(1'767'225'600)) + std::chrono::microseconds(250'000);
    }
};

class CalibrationTest : public ::testing::Test {
protected:
    void SetUp() override {
        directory = fs::temp_directory_path() / ("gateway-calibration-" + std::to_string(::getpid()));

        fs::remove_all(directory);
        fs::create_directories(directory);

        path = (directory / "sub" / "calibration.json").string();
    }

    void TearDown() override {
        fs::remove_all(directory);
    }

    void write(
        const std::string& text
    ) {
        fs::create_directories(fs::path(path).parent_path());

        std::ofstream(path) << text;
    }

    std::string read() const {
        std::ifstream file(path);
        std::ostringstream content;

        content << file.rdbuf();

        return content.str();
    }

    fs::path directory;
    std::string path;
    FixedClock clock;
};

const Json CALIBRATION = Json::parse(R"({
    "camera_to_robot": [[1.0, 0.0, 0.0, 400.0], [0.0, 1.0, 0.0, 0.0], [0.0, 0.0, 1.0, 600.0], [0.0, 0.0, 0.0, 1.0]],
    "units": "mm"
})");

TEST_F(CalibrationTest, NothingUntilUploaded) {
    EXPECT_FALSE(CalibrationService(path).get().has_value());
}

TEST_F(CalibrationTest, StoresWritesAndLoadsAgain) {
    CalibrationService service(path, clock);

    const StoredCalibration stored = service.set(CALIBRATION, "admin");

    EXPECT_EQ(stored.updated_by, "admin");
    EXPECT_EQ(stored.to_json()["updated_at"], "2026-01-01T00:00:00.250000Z");
    EXPECT_EQ(service.get()->calibration, CALIBRATION);

    //
    // Written atomically: the final file, no temporary left.
    //
    EXPECT_TRUE(fs::exists(path));
    EXPECT_FALSE(fs::exists(path + ".tmp"));

    const CalibrationService reloaded(path);

    ASSERT_TRUE(reloaded.get().has_value());
    EXPECT_EQ(reloaded.get()->to_json(), stored.to_json());
}

TEST_F(CalibrationTest, FileHasThePythonFormat) {
    CalibrationService(path, clock).set(CALIBRATION, "primary");

    const Json file = Json::parse(read());

    ASSERT_EQ(file.size(), 3u);
    EXPECT_EQ(file["calibration"], CALIBRATION);
    EXPECT_EQ(file["updated_at"], "2026-01-01T00:00:00.250000Z");
    EXPECT_EQ(file["updated_by"], "primary");

    //
    // Indented by two, as model_dump_json(indent=2).
    //
    EXPECT_EQ(read().substr(0, 18), "{\n  \"calibration\":");
}

TEST_F(CalibrationTest, ReadsAFileThePythonGatewayWrote) {
    write(R"({
  "calibration": {"units": "mm", "scale": 1.5},
  "updated_at": "2026-11-09T10:20:30.123456Z",
  "updated_by": "admin"
})");

    const auto stored = CalibrationService(path).get();

    ASSERT_TRUE(stored.has_value());
    EXPECT_EQ(stored->calibration["scale"], 1.5);
    EXPECT_EQ(stored->updated_by, "admin");
    EXPECT_EQ(format_json_time(stored->updated_at), "2026-11-09T10:20:30.123456Z");
}

class InvalidCalibrationFile : public CalibrationTest, public ::testing::WithParamInterface<const char*> {};

TEST_P(InvalidCalibrationFile, StopsStartup) {
    write(GetParam());

    EXPECT_THROW(CalibrationService{path}, CalibrationError);
}

INSTANTIATE_TEST_SUITE_P(
    Calibration,
    InvalidCalibrationFile,
    ::testing::Values(
        "",
        "{broken",
        "[]",
        R"({"updated_at": "2026-11-09T10:20:30Z", "updated_by": "a"})",
        R"({"calibration": [1], "updated_at": "2026-11-09T10:20:30Z", "updated_by": "a"})",
        R"({"calibration": {}, "updated_at": "yesterday", "updated_by": "a"})",
        R"({"calibration": {}, "updated_at": "2026-11-09T10:20:30Z", "updated_by": 7})",
        R"({"calibration": {}, "updated_by": "a"})"
    )
);

TEST(PythonJsonLength, MatchesJsonDumps) {
    //
    // Lengths from len(json.dumps(value)) in Python.
    //
    EXPECT_EQ(
        python_json_length(Json::parse(R"({"camera_to_robot": [[1.0, 0.0, 0.0, 400.0], [0.0, 1.0, 0.0, 0.0]], "units": "mm"})")),
        82u
    );

    EXPECT_EQ(
        python_json_length(
            Json::parse(
                "{\"name\": \"k\xc3\xa4mera \xe2\x98\x83 \xf0\x9f\x98\x80\", \"tab\": \"a\\tb\\n\\\"q\\\"\", "
                "\"n\": null, \"t\": true, \"f\": false, \"i\": -12, \"x\": 0.1}"
            )
        ),
        119u
    );

    EXPECT_EQ(
        python_json_length(Json::parse(R"({"empty": {}, "list": [], "nested": {"a": [1, [2, {"b": 3}]]}})")),
        62u
    );
}

}  // namespace
}  // namespace gw
