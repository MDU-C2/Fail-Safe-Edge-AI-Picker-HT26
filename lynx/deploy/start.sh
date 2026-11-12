#!/bin/sh
set -eu
export PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# WeOS rootfs packages do not retain Docker ENV metadata.
export PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
export OMP_NUM_THREADS="${OMP_NUM_THREADS:-1}" OPENBLAS_NUM_THREADS="${OPENBLAS_NUM_THREADS:-1}"
export MKL_NUM_THREADS="${MKL_NUM_THREADS:-1}" NUMEXPR_NUM_THREADS="${NUMEXPR_NUM_THREADS:-1}"
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"
export GATEWAY_SECURITY_MODE=strict GATEWAY_CONTROL_REQUIRE_SESSION=true
export GATEWAY_AUTH_CLIENTS_FILE="${GATEWAY_AUTH_CLIENTS_FILE:-/state/auth_clients.json}"
export GATEWAY_CALIBRATION_FILE="${GATEWAY_CALIBRATION_FILE:-/state/gateway-calibration.json}"
cd /app
exec python -m lynx.app \
  --state-dir /state --model /opt/model --worker /usr/local/bin/lynx-ncnn-worker "$@"
