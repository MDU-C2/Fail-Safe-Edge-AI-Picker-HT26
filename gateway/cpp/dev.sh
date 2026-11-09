#!/bin/sh
# ================================================================
# Build and test the C++ gateway in the toolchain container
# ================================================================
#
# Run in WSL (or any Linux with Docker), from anywhere:
#
#   gateway/cpp/dev.sh toolchain            build the toolchain image (once)
#   gateway/cpp/dev.sh build [PRESET]       configure + build (default: dev)
#   gateway/cpp/dev.sh test [PRESET]        C++ unit tests (ctest)
#   gateway/cpp/dev.sh pytest [ARGS...]     Python suite against the C++ binary
#   gateway/cpp/dev.sh pytest-python [ARGS] Python suite against the Python gateway (Linux)
#   gateway/cpp/dev.sh sanitize             unit tests + suite under ASan/UBSan and TSan
#   gateway/cpp/dev.sh image                runtime image, with its sizes
#   gateway/cpp/dev.sh shell                a shell in the toolchain
#
# The repository is mounted at /src; the build trees live in the
# Docker volume gateway-cpp-build (building on /mnt/c is slow).
# PRESET is one of CMakePresets.json: dev, release-size, asan, tsan.
#
# From Windows: wsl -d Ubuntu-22.04 -u root -- gateway/cpp/dev.sh build
#
set -eu

REPO=$(cd "$(dirname "$0")/../.." && pwd)
IMAGE=gateway-cpp-toolchain
VOLUME=gateway-cpp-build
DOCKERFILE=gateway/cpp/docker/Dockerfile

#
# Host networking: with WSL's mirrored networking (networkingMode=
# mirrored in %USERPROFILE%\.wslconfig) 127.0.0.1 is then Windows, so
# pytest --robotstudio reaches the RobotStudio virtual controller.
#
run() {
    docker run --rm \
        --network host \
        -v "$REPO:/src" \
        -v "$VOLUME:/src/gateway/cpp/build" \
        -e PYTHONDONTWRITEBYTECODE=1 \
        -w /src \
        "$IMAGE" "$@"
}

command=${1:-build}
[ $# -gt 0 ] && shift

case "$command" in
    toolchain)
        cd "$REPO"
        docker build -f "$DOCKERFILE" --target toolchain -t "$IMAGE" .
        ;;

    build)
        preset=${1:-dev}
        run sh -c "cmake --preset $preset -S gateway/cpp >/dev/null && cmake --build gateway/cpp/build/$preset"
        ;;

    test)
        preset=${1:-dev}
        run sh -c "cmake --build gateway/cpp/build/$preset && ctest --test-dir gateway/cpp/build/$preset --output-on-failure"
        ;;

    pytest)
        preset=${GATEWAY_CPP_PRESET:-dev}
        run sh -c "cmake --build gateway/cpp/build/$preset >/dev/null && \
            GATEWAY_IMPL=cpp GATEWAY_CPP_BINARY=gateway/cpp/build/$preset/gateway \
            python -m pytest -p no:cacheprovider $*"
        ;;

    pytest-python)
        run sh -c "GATEWAY_IMPL=python python -m pytest -p no:cacheprovider $*"
        ;;

    sanitize)
        #
        # Every gateway the suite starts runs sanitized; a report in
        # any gateway.log fails the run. TSan cannot map its shadow
        # memory on arm64 with address randomisation, hence setarch -R
        # (which needs the default seccomp profile lifted).
        #
        docker run --rm \
            --network host \
            --security-opt seccomp=unconfined \
            -v "$REPO:/src" \
            -v "$VOLUME:/src/gateway/cpp/build" \
            -e PYTHONDONTWRITEBYTECODE=1 \
            -e ASAN_OPTIONS=detect_leaks=0 \
            -e TSAN_OPTIONS=halt_on_error=0 \
            -w /src \
            "$IMAGE" sh -c '
                status=0
                for preset in asan tsan; do
                    echo "===== $preset ====="
                    cmake --preset $preset -S gateway/cpp > /dev/null &&
                    cmake --build gateway/cpp/build/$preset > /dev/null || { status=1; continue; }
                    rm -rf /tmp/pytest-$preset
                    setarch "$(uname -m)" -R sh -c "
                        ctest --test-dir gateway/cpp/build/$preset --output-on-failure | tail -3 &&
                        python -m pytest -p no:cacheprovider -q -rf --impl cpp \
                            --cpp-binary gateway/cpp/build/$preset/gateway --basetemp /tmp/pytest-$preset \
                            | grep -E \"^FAILED|passed|failed\"
                    " || status=1
                    if grep -l -E "Sanitizer|runtime error" -r /tmp/pytest-$preset --include gateway.log; then
                        grep -h -A25 -E "WARNING: ThreadSanitizer|ERROR: AddressSanitizer|runtime error" \
                            -r /tmp/pytest-$preset --include gateway.log | head -80
                        status=1
                    fi
                done
                exit $status
            '
        ;;

    image)
        cd "$REPO"
        docker build -f "$DOCKERFILE" -t gateway-cpp .
        docker images gateway-cpp
        ;;

    shell)
        docker run --rm -it \
            -v "$REPO:/src" \
            -v "$VOLUME:/src/gateway/cpp/build" \
            -w /src \
            "$IMAGE" bash
        ;;

    *)
        sed -n '2,22p' "$0"
        exit 2
        ;;
esac
