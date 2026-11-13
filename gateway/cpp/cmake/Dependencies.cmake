# ================================================================
# Dependencies
# ================================================================
#
# Pinned release archives, fetched once into the build tree.
#

include(FetchContent)

set(FETCHCONTENT_QUIET ON)

find_package(Threads REQUIRED)

# ----------------------------------------------------------------
# OpenSSL: TLS for the gateway's own server (TLS.md)
# ----------------------------------------------------------------
#
# Debian's OpenSSL 3 (libssl-dev in the toolchain image), its static
# archives for the static release binary. Only the server uses it;
# the RWS client (curl below) stays plain HTTP.
#

if(GATEWAY_STATIC)
    set(OPENSSL_USE_STATIC_LIBS TRUE)
endif()

find_package(OpenSSL 3.0 REQUIRED)

# ----------------------------------------------------------------
# cpp-httplib: the HTTP(S) and WebSocket server
# ----------------------------------------------------------------
#
# One header, copied to generated/gw_httplib/httplib.h so that it is
# never confused with depthai-core's older httplib.h in its vcpkg
# include directory. src/http/httplib.hpp includes it (under the
# namespace gw_httplib, see there).
#

FetchContent_Declare(
    cpp_httplib
    URL https://github.com/yhirose/cpp-httplib/archive/refs/tags/v0.63.0.tar.gz
    URL_HASH SHA256=44ab42e16926e95f7641d286bc90d739ab1c16886114d7cc5bc7a9f905c5edf8
    DOWNLOAD_EXTRACT_TIMESTAMP ON
    #
    # Download only; its own CMake project is not used.
    #
    SOURCE_SUBDIR no-cmake-project
)

FetchContent_MakeAvailable(cpp_httplib)

configure_file(
    ${cpp_httplib_SOURCE_DIR}/httplib.h
    ${CMAKE_CURRENT_BINARY_DIR}/generated/gw_httplib/httplib.h
    COPYONLY
)

add_library(gateway_httplib INTERFACE)

target_include_directories(
    gateway_httplib
    INTERFACE
    ${CMAKE_CURRENT_BINARY_DIR}/generated
)

target_link_libraries(
    gateway_httplib
    INTERFACE
    OpenSSL::SSL
    OpenSSL::Crypto
)

#
# Debian's static libcrypto is built with zstd (certificate
# compression), which its CMake target does not name.
#
if(GATEWAY_STATIC)
    find_library(GATEWAY_ZSTD_STATIC NAMES libzstd.a REQUIRED)

    target_link_libraries(
        gateway_httplib
        INTERFACE
        ${GATEWAY_ZSTD_STATIC}
    )
endif()

# ----------------------------------------------------------------
# nlohmann/json
# ----------------------------------------------------------------

#
# With depthai-core, its own copy, so the binary has one version.
#
if(GATEWAY_WITH_DEPTHAI)
    find_package(nlohmann_json 3.11 CONFIG REQUIRED)
else()
    FetchContent_Declare(
        nlohmann_json
        URL https://github.com/nlohmann/json/releases/download/v3.11.3/json.tar.xz
        URL_HASH SHA256=d6c65aca6b1ed68e7a182f4757257b107ae403032760ed6ef121c9d55e81757d
        DOWNLOAD_EXTRACT_TIMESTAMP ON
    )

    set(JSON_Install OFF CACHE INTERNAL "")

    FetchContent_MakeAvailable(nlohmann_json)
endif()

# ----------------------------------------------------------------
# libcurl: Robot Web Services client (HTTP + digest auth + cookies)
# ----------------------------------------------------------------
#
# Built static with HTTP only and no TLS, compression, IDN, proxy
# or other authentication schemes: the controller speaks plain HTTP
# with digest auth on the robot network.
#

foreach(
    option
    BUILD_CURL_EXE
    BUILD_SHARED_LIBS
    BUILD_TESTING
    BUILD_LIBCURL_DOCS
    BUILD_MISC_DOCS
    ENABLE_CURL_MANUAL
    CURL_ENABLE_SSL
    CURL_USE_OPENSSL
    CURL_USE_LIBPSL
    CURL_USE_LIBSSH2
    CURL_USE_LIBSSH
    CURL_USE_GSSAPI
    CURL_ZLIB
    CURL_BROTLI
    CURL_ZSTD
    USE_NGHTTP2
    USE_LIBIDN2
    USE_LIBRTMP
    ENABLE_UNIX_SOCKETS
    PICKY_COMPILER
)
    set(${option} OFF CACHE INTERNAL "")
endforeach()

foreach(
    option
    BUILD_STATIC_LIBS
    HTTP_ONLY
    CURL_DISABLE_ALTSVC
    CURL_DISABLE_AWS
    CURL_DISABLE_BASIC_AUTH
    CURL_DISABLE_BEARER_AUTH
    CURL_DISABLE_DOH
    CURL_DISABLE_HSTS
    CURL_DISABLE_KERBEROS_AUTH
    CURL_DISABLE_MIME
    CURL_DISABLE_NEGOTIATE_AUTH
    CURL_DISABLE_NETRC
    CURL_DISABLE_NTLM
    CURL_DISABLE_PROGRESS_METER
    CURL_DISABLE_PROXY
    CURL_DISABLE_WEBSOCKETS
)
    set(${option} ON CACHE INTERNAL "")
endforeach()

FetchContent_Declare(
    curl
    URL https://github.com/curl/curl/releases/download/curl-8_11_1/curl-8.11.1.tar.xz
    DOWNLOAD_EXTRACT_TIMESTAMP ON
    EXCLUDE_FROM_ALL
)

FetchContent_MakeAvailable(curl)

# ----------------------------------------------------------------
# GoogleTest
# ----------------------------------------------------------------

if(GATEWAY_BUILD_TESTS)
    set(INSTALL_GTEST OFF CACHE INTERNAL "")
    set(BUILD_GMOCK ON CACHE INTERNAL "")

    FetchContent_Declare(
        googletest
        URL https://github.com/google/googletest/releases/download/v1.15.2/googletest-1.15.2.tar.gz
        DOWNLOAD_EXTRACT_TIMESTAMP ON
        EXCLUDE_FROM_ALL
    )

    FetchContent_MakeAvailable(googletest)
endif()
