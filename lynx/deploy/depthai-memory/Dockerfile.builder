FROM python:3.11-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential git curl ca-certificates ninja-build pkg-config zip unzip \
    autoconf automake libtool nasm bison gfortran libarchive-dev && \
    rm -rf /var/lib/apt/lists/*
RUN python -m pip install --no-cache-dir \
    cmake==3.30.0 setuptools==75.6.0 wheel==0.45.1 \
    numpy==1.26.4 jinja2==3.1.6 auditwheel==6.4.2 patchelf==0.17.2.4
ENV CMAKE_BUILD_PARALLEL_LEVEL=2 VCPKG_MAX_CONCURRENCY=2 \
    OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
WORKDIR /src
