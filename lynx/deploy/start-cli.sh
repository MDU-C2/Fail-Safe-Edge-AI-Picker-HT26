#!/bin/sh
set -eu
export PATH=/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# WeOS rootfs packages do not retain Docker environment metadata.
export PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 MALLOC_ARENA_MAX=2
export OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
cd /app
if [ "${1-}" = "--serve" ]; then
  shift
  export PYTHONPATH=/app:/app/capture_and_detect
  exec python -m device_service --device-id lynx --backend native \
    --model /opt/model --worker /usr/local/bin/lynx-ncnn-worker "$@"
fi
exec python -m lynx.picker --state-dir /state --model /opt/model \
  --worker /usr/local/bin/lynx-ncnn-worker "$@"
