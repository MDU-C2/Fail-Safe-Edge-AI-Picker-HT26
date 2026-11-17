# Build ONLY from the context produced by prepare_context.py, never the repo root.
# Replace PYTHON_IMAGE with a validated digest when freezing a deployed release.
ARG PYTHON_IMAGE=python:3.11-slim-bookworm
FROM ${PYTHON_IMAGE} AS native-build
ARG TARGETARCH
ARG NCNN_RELEASE=20260526
ARG NCNN_SHA256=754659d6fe65545cf2ef4483ffb84526fea631f8764c44b150f1601d0fb4004b
ARG OPENCV_VERSION=4.12.0
ARG OPENCV_SHA256=44c106d5bb47efec04e531fd93008b3fcd1d27138985c5baf4eafac0e1ec9e9d
RUN test "$TARGETARCH" = arm64
RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential cmake ninja-build curl ca-certificates unzip && \
    rm -rf /var/lib/apt/lists/*
RUN mkdir -p /src /licenses && \
    curl --fail --location --retry 3 \
      "https://github.com/Tencent/ncnn/releases/download/${NCNN_RELEASE}/ncnn-${NCNN_RELEASE}-full-source.zip" \
      -o /tmp/ncnn.zip && \
    echo "${NCNN_SHA256}  /tmp/ncnn.zip" | sha256sum -c - && \
    unzip -q /tmp/ncnn.zip -d /src && \
    HEADER="$(find /src -path '*/src/net.h' -print -quit)" && test -n "$HEADER" && \
    SRC="${HEADER%/src/net.h}" && test -f "$SRC/CMakeLists.txt" && \
    cp "$SRC/LICENSE.txt" /licenses/ncnn-LICENSE.txt && \
    cmake -S "$SRC" -B /ncnn-build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/ncnn \
      -DCMAKE_C_FLAGS='-march=armv8-a -mtune=generic' \
      -DCMAKE_CXX_FLAGS='-march=armv8-a -mtune=generic' \
      -DNCNN_VERSION=${NCNN_RELEASE} -DNCNN_SHARED_LIB=OFF \
      -DNCNN_VULKAN=OFF -DNCNN_OPENMP=OFF -DNCNN_THREADS=OFF \
      -DNCNN_RUNTIME_CPU=OFF -DNCNN_ARM82=OFF -DNCNN_ARM82DOT=OFF \
      -DNCNN_ARM82FP16FML=OFF -DNCNN_ARM84BF16=OFF -DNCNN_ARM84I8MM=OFF \
      -DNCNN_ARM86SVE=OFF -DNCNN_INT8=ON -DNCNN_BF16=OFF -DNCNN_C_API=OFF \
      -DNCNN_PIXEL_ROTATE=OFF -DNCNN_PIXEL_AFFINE=OFF -DNCNN_PIXEL_DRAWING=OFF \
      -DNCNN_BUILD_TOOLS=OFF -DNCNN_BUILD_EXAMPLES=OFF \
      -DNCNN_BUILD_BENCHMARK=OFF -DNCNN_BUILD_TESTS=OFF -DNCNN_PYTHON=OFF && \
    cmake --build /ncnn-build --parallel 2 && cmake --install /ncnn-build
# Debian's core/imgproc packages pull Mesa/X11/LLVM. Link only these two modules
# from a minimal static build instead; the worker needs no GUI or image codecs.
RUN curl --fail --location --retry 3 \
      "https://github.com/opencv/opencv/archive/refs/tags/${OPENCV_VERSION}.tar.gz" \
      -o /tmp/opencv.tar.gz && \
    echo "${OPENCV_SHA256}  /tmp/opencv.tar.gz" | sha256sum -c - && \
    tar -xzf /tmp/opencv.tar.gz -C /src && \
    cp "/src/opencv-${OPENCV_VERSION}/LICENSE" /licenses/opencv-LICENSE.txt && \
    cmake -S "/src/opencv-${OPENCV_VERSION}" -B /opencv-build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/opt/opencv \
      -DCMAKE_C_FLAGS='-march=armv8-a -mtune=generic' \
      -DCMAKE_CXX_FLAGS='-march=armv8-a -mtune=generic' \
      -DBUILD_LIST=core,imgproc -DBUILD_SHARED_LIBS=OFF \
      -DCPU_BASELINE=NEON -DCPU_DISPATCH= \
      -DBUILD_TESTS=OFF -DBUILD_PERF_TESTS=OFF -DBUILD_EXAMPLES=OFF \
      -DBUILD_opencv_apps=OFF -DBUILD_opencv_python2=OFF -DBUILD_opencv_python3=OFF \
      -DBUILD_JAVA=OFF -DBUILD_ZLIB=ON \
      -DWITH_ADE=OFF -DWITH_KLEIDICV=OFF -DWITH_CAROTENE=OFF \
      -DWITH_PROTOBUF=OFF -DBUILD_PROTOBUF=OFF -DWITH_FLATBUFFERS=OFF -DBUILD_FLATBUFFERS=OFF \
      -DWITH_OPENCL=OFF -DWITH_OPENGL=OFF -DWITH_GTK=OFF -DWITH_QT=OFF \
      -DWITH_FFMPEG=OFF -DWITH_GSTREAMER=OFF -DWITH_V4L=OFF \
      -DWITH_TBB=OFF -DWITH_OPENMP=OFF -DWITH_PTHREADS_PF=OFF \
      -DWITH_LAPACK=OFF -DWITH_EIGEN=OFF -DWITH_IPP=OFF -DWITH_ITT=OFF \
      -DWITH_JPEG=OFF -DWITH_PNG=OFF -DWITH_TIFF=OFF -DWITH_WEBP=OFF \
      -DWITH_JASPER=OFF -DBUILD_JASPER=OFF -DWITH_AVIF=OFF -DWITH_JPEGXL=OFF \
      -DWITH_SPNG=OFF -DWITH_QUIRC=OFF -DWITH_VTK=OFF \
      -DWITH_OPENJPEG=OFF -DWITH_OPENEXR=OFF && \
    cmake --build /opencv-build --parallel 2 && cmake --install /opencv-build
COPY native/ /src/worker/
RUN cmake -S /src/worker -B /worker-build -G Ninja \
      -DCMAKE_BUILD_TYPE=Release -DCMAKE_PREFIX_PATH='/opt/ncnn;/opt/opencv' \
      -DCMAKE_CXX_FLAGS='-march=armv8-a -mtune=generic' \
      -DCMAKE_INSTALL_PREFIX=/out && \
    cmake --build /worker-build --parallel 2 && \
    ctest --test-dir /worker-build --output-on-failure && \
    cmake --install /worker-build && strip /out/bin/lynx-ncnn-worker

FROM ${PYTHON_IMAGE} AS python-build
COPY requirements-arm64.lock /tmp/requirements-arm64.lock
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --only-binary=:all: --require-hashes \
      -r /tmp/requirements-arm64.lock

FROM ${PYTHON_IMAGE}
ENV PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
    PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1 \
    NUMEXPR_NUM_THREADS=1 MALLOC_ARENA_MAX=2 \
    GATEWAY_SECURITY_MODE=strict GATEWAY_CONTROL_REQUIRE_SESSION=true \
    GATEWAY_AUTH_CLIENTS_FILE=/state/auth_clients.json \
    GATEWAY_CALIBRATION_FILE=/state/gateway-calibration.json
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates libstdc++6 libusb-1.0-0 libudev1 && \
    rm -rf /var/lib/apt/lists/*
COPY --from=python-build /opt/venv/ /opt/venv/
COPY --from=native-build /out/bin/lynx-ncnn-worker /usr/local/bin/lynx-ncnn-worker
COPY --from=native-build /licenses/ /usr/share/licenses/lynx-picker/
WORKDIR /app
COPY app/ /app/
COPY model/ /opt/model/
COPY source-manifest.json /usr/share/lynx-picker/source-manifest.json
COPY requirements-arm64.lock /usr/share/lynx-picker/requirements-arm64.lock
COPY start.sh /start.sh
COPY measure_resources.py /usr/local/bin/lynx-measure-resources
RUN chmod 0755 /start.sh /usr/local/bin/lynx-measure-resources && \
    /usr/local/bin/lynx-ncnn-worker --help && \
    python -c "import cv2, depthai, fastapi, numpy, pydantic_settings, requests, uvicorn; import importlib.util; assert importlib.util.find_spec('torch') is None; assert importlib.util.find_spec('ultralytics') is None"
EXPOSE 8080
ENTRYPOINT ["/start.sh"]
