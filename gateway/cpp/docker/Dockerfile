# C++ gateway: toolchain, build and runtime images.
#
# Build context is the repository root, like gateway/docker/DockerFile:
#
#   docker build -f gateway/cpp/docker/Dockerfile --target toolchain -t gateway-cpp-toolchain .
#   docker build -f gateway/cpp/docker/Dockerfile -t gateway-cpp .
#
# gateway/cpp/dev.sh wraps the toolchain image for day-to-day builds
# and tests. Only what Dockerfile.dockerignore lets through is sent.

# ================================================================
# Toolchain: compilers, header-only Boost, Python for the test suite
# ================================================================

FROM debian:trixie-slim AS toolchain

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        ca-certificates \
        cmake \
        git \
        libboost-dev \
        ninja-build \
        python3 \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

#
# The pytest suite, and the Python gateway's own packages so that
# every test module imports (some import gateway code). depthai is
# left out: the C++ gateway brings its own and the OAK unit tests
# skip without it.
#
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir \
        fastapi \
        pydantic-settings \
        pytest \
        requests \
        "uvicorn[standard]" \
        websockets

ENV PATH=/opt/venv/bin:$PATH

WORKDIR /src

# ================================================================
# depthai-core: built once, as small as it goes
# ================================================================
#
# The same version as the Python gateway's wheel. Static library
# with everything the gateway does not use switched off: no OpenCV,
# no network/model-zoo (curl), no protobuf/remote connection, no
# RVC4 (OAK 4) firmware, no video recording, no xtensor, no
# dynamic calibration, no AprilTag, no stack traces; USB off (the
# camera is PoE). Its dependencies come from its own vcpkg.
#

FROM toolchain AS depthai

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        autoconf \
        automake \
        curl \
        libtool \
        pkg-config \
        tar \
        unzip \
        zip \
    && rm -rf /var/lib/apt/lists/*

ARG DEPTHAI_VERSION=v3.10.0

#
# vcpkg cannot download its own tools on arm64.
#
ENV VCPKG_FORCE_SYSTEM_BINARIES=1

#
# Keeps the device firmware out of RAM once the camera has booted
# (~125 MB otherwise); see the patch's header.
#
COPY gateway/cpp/docker/depthai-memory.patch /tmp/depthai-memory.patch

RUN git clone --depth 1 --branch "$DEPTHAI_VERSION" --recurse-submodules --shallow-submodules \
        https://github.com/luxonis/depthai-core.git /tmp/depthai-core \
    && git -C /tmp/depthai-core apply /tmp/depthai-memory.patch \
    && cmake -S /tmp/depthai-core -B /tmp/depthai-build -G Ninja \
        -DCMAKE_BUILD_TYPE=MinSizeRel \
        -DCMAKE_C_FLAGS="-ffunction-sections -fdata-sections" \
        -DCMAKE_CXX_FLAGS="-ffunction-sections -fdata-sections" \
        -DCMAKE_INSTALL_PREFIX=/opt/depthai \
        -DBUILD_SHARED_LIBS=OFF \
        -DDEPTHAI_BUILD_TESTS=OFF \
        -DDEPTHAI_BUILD_EXAMPLES=OFF \
        -DDEPTHAI_BUILD_PYTHON=OFF \
        -DDEPTHAI_BUILD_DOCS=OFF \
        -DDEPTHAI_BUILD_ZOO_HELPER=OFF \
        -DDEPTHAI_BUILD_BETA=OFF \
        -DDEPTHAI_CLANG_FORMAT=OFF \
        -DDEPTHAI_OPENCV_SUPPORT=OFF \
        -DDEPTHAI_XTENSOR_SUPPORT=OFF \
        -DDEPTHAI_PCL_SUPPORT=OFF \
        -DDEPTHAI_RTABMAP_SUPPORT=OFF \
        -DDEPTHAI_BASALT_SUPPORT=OFF \
        -DDEPTHAI_DYNAMIC_CALIBRATION_SUPPORT=OFF \
        -DDEPTHAI_ENABLE_CURL=OFF \
        -DDEPTHAI_ENABLE_PROTOBUF=OFF \
        -DDEPTHAI_ENABLE_REMOTE_CONNECTION=OFF \
        -DDEPTHAI_ENABLE_EVENTS_MANAGER=OFF \
        -DDEPTHAI_ENABLE_MP4V2=OFF \
        -DDEPTHAI_ENABLE_APRIL_TAG=OFF \
        -DDEPTHAI_ENABLE_KOMPUTE=OFF \
        -DDEPTHAI_ENABLE_BACKWARD=OFF \
        -DDEPTHAI_ENABLE_LIBUSB=OFF \
        -DDEPTHAI_ENABLE_DEVICE_RVC4_FW=OFF \
        -DDEPTHAI_ENABLE_DEVICE_RVC3_FW=OFF \
    && cmake --build /tmp/depthai-build \
    && cmake --install /tmp/depthai-build \
    && mkdir -p /opt/depthai/vcpkg \
    && cp -a /tmp/depthai-build/vcpkg_installed/*-linux/. /opt/depthai/vcpkg/ \
    && rm -rf /opt/depthai/vcpkg/debug \
    && rm -rf /tmp/depthai-core /tmp/depthai-build /root/.cache/vcpkg

# ================================================================
# Toolchain with depthai-core: what dev.sh builds and tests in
# ================================================================

FROM toolchain AS toolchain-oak

COPY --from=depthai /opt/depthai /opt/depthai

#
# The debug libraries were left out; so must be the CMake files that
# point at them.
#
RUN find /opt/depthai/vcpkg/share -name "*-debug.cmake" -delete

#
# depthai-core, and the libraries it links statically (fmt, spdlog,
# libarchive, ...) from its vcpkg.
#
ENV CMAKE_PREFIX_PATH=/opt/depthai:/opt/depthai/vcpkg

#
# TLS for the gateway's own server (TLS.md): Debian's OpenSSL 3,
# linked statically from libssl-dev's archives (its libcrypto needs
# zstd for certificate compression), and trustme, which
# gives the test suite a throwaway CA; rich for the diagnostics
# tool's tests. Installed here rather than in the toolchain stage so
# the depthai-core build stays cached.
#
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libssl-dev \
        libzstd-dev \
    && rm -rf /var/lib/apt/lists/* \
    && /opt/venv/bin/pip install --no-cache-dir \
        rich \
        trustme

# ================================================================
# Build: the size-optimised release binary
# ================================================================

FROM toolchain-oak AS build

COPY gateway/cpp /src/gateway/cpp

RUN cmake --preset release-size -S gateway/cpp \
    && cmake --build gateway/cpp/build/release-size \
    && ctest --test-dir gateway/cpp/build/release-size --output-on-failure

# ================================================================
# Runtime: one static binary, nothing else
# ================================================================

FROM scratch AS runtime

#
# WeOS app hosting starts /sbin/init unless told otherwise, so that
# is where the binary lives. Settings come from GATEWAY_* variables
# (or a mounted gateway/.env), the clients file from
# GATEWAY_AUTH_CLIENTS_FILE.
#
COPY --from=build /src/gateway/cpp/build/release-size/gateway /sbin/init

#
# The clients file at its default path (relative to /), as the Python
# image has it; only token hashes. A WeOS package keeps just the
# filesystem, not ENTRYPOINT, so the app's cmd must give the
# arguments: cmd "/sbin/init --host 0.0.0.0 --port 8000".
#
COPY gateway/auth_clients.json /gateway/auth_clients.json

WORKDIR /

EXPOSE 8000

ENTRYPOINT ["/sbin/init", "--host", "0.0.0.0", "--port", "8000"]
