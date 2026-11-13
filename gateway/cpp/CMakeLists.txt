cmake_minimum_required(VERSION 3.25)

project(
    gateway_cpp
    VERSION 0.5.0
    LANGUAGES C CXX
)

# ================================================================
# Options
# ================================================================

option(GATEWAY_BUILD_TESTS "Build the GoogleTest unit tests" ON)
option(GATEWAY_STATIC "Link the gateway binary fully statically" OFF)
option(GATEWAY_SIZE "Optimise for size: -Os, LTO, section GC, strip" OFF)
option(GATEWAY_WITH_DEPTHAI "Build the OAK camera producer with depthai-core (else mock camera only)" OFF)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(CMAKE_EXPORT_COMPILE_COMMANDS ON)

include(cmake/Size.cmake)
include(cmake/Dependencies.cmake)
include(cmake/Embed.cmake)

# ================================================================
# Embedded files
# ================================================================

#
# The API contract, exported from the Python gateway with
# tools/export_openapi.py and served as is at /openapi.json.
#
gateway_embed_file(
    ${CMAKE_CURRENT_SOURCE_DIR}/openapi/openapi.json
    openapi_json
    ${CMAKE_CURRENT_BINARY_DIR}/generated/openapi_json.cpp
)

# ================================================================
# Library: everything but main(), so the unit tests can link it
# ================================================================

add_library(
    gateway_core
    STATIC
    src/app.cpp
    src/calibration/routes.cpp
    src/calibration/service.cpp
    src/camera/mock_producer.cpp
    src/camera/oak_producer.cpp
    src/camera/routes.cpp
    src/camera/service.cpp
    src/config/dotenv.cpp
    src/config/settings.cpp
    src/control/manager.cpp
    src/control/routes.cpp
    src/events/bus.cpp
    src/events/routes.cpp
    src/health.cpp
    src/http/router.cpp
    src/http/server.cpp
    src/http/validate.cpp
    src/robot/routes.cpp
    src/robot/service.cpp
    src/rws/arms_routes.cpp
    src/rws/client.cpp
    src/rws/errors.cpp
    src/rws/motion.cpp
    src/rws/motion_routes.cpp
    src/rws/rapid.cpp
    src/rws/rapid_routes.cpp
    src/security/auth.cpp
    src/security/client_registry.cpp
    src/util/log.cpp
    src/util/sha256.cpp
    src/util/text.cpp
    src/util/thread.cpp
    src/util/time.cpp
    ${CMAKE_CURRENT_BINARY_DIR}/generated/openapi_json.cpp
)

target_include_directories(
    gateway_core
    PUBLIC
    src
)

target_compile_definitions(
    gateway_core
    PUBLIC
    GATEWAY_VERSION="${PROJECT_VERSION}"
)

target_link_libraries(
    gateway_core
    PUBLIC
    gateway_httplib
    nlohmann_json::nlohmann_json
    CURL::libcurl
    Threads::Threads
)

gateway_warnings(gateway_core)

#
# The OAK camera: the DepthAI implementation, or a stub that makes
# GATEWAY_CAMERA_PROVIDER=oak refuse to start.
#
if(GATEWAY_WITH_DEPTHAI)
    #
    # A static depthai-core names its private dependencies in its
    # link interface but does not look them up itself; they come
    # from its vcpkg tree (/opt/depthai/vcpkg in the toolchain).
    #
    foreach(package fmt yaml-cpp spdlog httplib semver magic_enum lz4 liblzma Eigen3)
        find_package(${package} CONFIG REQUIRED)
    endforeach()

    find_package(BZip2 REQUIRED)
    find_package(ZLIB REQUIRED)
    find_package(LibArchive REQUIRED)

    find_package(depthai CONFIG REQUIRED)

    target_sources(gateway_core PRIVATE src/camera/depthai_device.cpp)
    target_link_libraries(gateway_core PUBLIC depthai::core)
    target_compile_definitions(gateway_core PUBLIC GATEWAY_WITH_DEPTHAI)

    #
    # tools/depthai_probe.cpp: depthai's RAM without a camera (only
    # built on request).
    #
    add_executable(
        depthai_probe
        EXCLUDE_FROM_ALL
        tools/depthai_probe.cpp
    )

    target_link_libraries(
        depthai_probe
        PRIVATE
        depthai::core
    )
else()
    target_sources(gateway_core PRIVATE src/camera/no_depthai.cpp)
endif()

# ================================================================
# Gateway binary
# ================================================================

add_executable(
    gateway
    src/main.cpp
)

target_link_libraries(
    gateway
    PRIVATE
    gateway_core
)

gateway_warnings(gateway)

if(GATEWAY_STATIC)
    target_link_options(
        gateway
        PRIVATE
        -static
    )
endif()

# ================================================================
# Tests
# ================================================================

if(GATEWAY_BUILD_TESTS)
    enable_testing()

    add_subdirectory(tests)
endif()
