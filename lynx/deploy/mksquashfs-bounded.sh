#!/bin/sh
# Packaging runs on the build machine, with bounded CPU and compression memory.
exec /usr/bin/mksquashfs "$@" -processors 2 -mem 256M
