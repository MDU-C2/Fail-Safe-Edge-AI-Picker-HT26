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
# Build: the size-optimised release binary
# ================================================================

FROM toolchain AS build

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

WORKDIR /

EXPOSE 8000

ENTRYPOINT ["/sbin/init", "--host", "0.0.0.0", "--port", "8000"]
