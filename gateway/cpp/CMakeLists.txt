cmake_minimum_required(VERSION 3.25)

project(
    gateway_cpp
    VERSION 0.3.0
    LANGUAGES C CXX
)

# ================================================================
# Options
# ================================================================

option(GATEWAY_BUILD_TESTS "Build the GoogleTest unit tests" ON)
option(GATEWAY_STATIC "Link the gateway binary fully statically" OFF)
option(GATEWAY_SIZE "Optimise for size: -Os, LTO, section GC, strip" OFF)

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
    src/camera/routes.cpp
    src/camera/service.cpp
    src/config/dotenv.cpp
    src/config/settings.cpp
    src/control/manager.cpp
    src/control/routes.cpp
    src/events/bus.cpp
    src/health.cpp
    src/http/router.cpp
    src/http/server.cpp
    src/robot/routes.cpp
    src/robot/service.cpp
    src/rws/client.cpp
    src/rws/errors.cpp
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
    BOOST_ASIO_NO_DEPRECATED
    BOOST_BEAST_USE_STD_STRING_VIEW
)

target_link_libraries(
    gateway_core
    PUBLIC
    Boost::headers
    nlohmann_json::nlohmann_json
    CURL::libcurl
    Threads::Threads
)

gateway_warnings(gateway_core)

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
