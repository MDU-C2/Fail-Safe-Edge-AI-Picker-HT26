#!/usr/bin/env python3
"""Offline firmware-cache RSS test: run with Docker --network none, no devices.

No Device instance is constructed. The static firmware getter has a known return
binding failure in the official 3.1.0 wheel, after its C++ work has completed.
"""
import ctypes
import gc
import json
import os
from pathlib import Path
import sys


def report(stage):
    values = {}
    for line in Path("/proc/self/smaps_rollup").read_text().splitlines():
        key, _, value = line.partition(":")
        if key in {"Rss", "Pss", "Pss_Anon", "Pss_File", "Pss_Shmem"}:
            values[key + "_bytes"] = int(value.split()[0]) * 1024
    print(json.dumps({"stage": stage, **values}), flush=True)


def main():
    # Both baseline and preload comparison use the same symbol visibility.
    # It lets the diagnostic wrapper resolve the original reader with RTLD_NEXT.
    sys.setdlopenflags(os.RTLD_NOW | os.RTLD_GLOBAL)
    import numpy
    import cv2
    import fastapi
    import pydantic_settings
    import requests
    import uvicorn
    import lynx
    import capture_and_detect
    import depthai

    cv2.setNumThreads(1)
    cv2.ocl.setUseOpenCL(False)
    report("imports")
    try:
        blob = depthai.Device.getEmbeddedDeviceBinary(False)
        print(json.dumps({"returned_bytes": len(blob)}), flush=True)
        del blob
    except TypeError as error:
        if "Unable to convert function return value" not in str(error):
            raise
        print(json.dumps({"known_wheel_binding_error": True}), flush=True)
    gc.collect()
    report("firmware_released")
    libc = ctypes.CDLL(None)
    libc.malloc_trim.argtypes = [ctypes.c_size_t]
    libc.malloc_trim.restype = ctypes.c_int
    print(json.dumps({"malloc_trim_return": libc.malloc_trim(0)}), flush=True)
    report("after_malloc_trim")


if __name__ == "__main__":
    main()
