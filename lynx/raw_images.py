"""Streaming PNG audit writer using only the Python standard library."""
from array import array
import os
from pathlib import Path
import struct
import tempfile
import zlib


def _chunk(stream, kind, data):
    stream.write(struct.pack(">I", len(data)))
    stream.write(kind)
    stream.write(data)
    stream.write(struct.pack(">I", zlib.crc32(data, zlib.crc32(kind)) & 0xffffffff))


def write_png(path, raw, width, height, *, depth=False):
    """BGR888p or little-endian RAW16; at most one scanline of conversion scratch."""
    expected = width * height * (2 if depth else 3)
    if type(width) is not int or type(height) is not int or not (0 < width <= 1920 and 0 < height <= 1080) or len(raw) != expected:
        raise ValueError("Invalid raw audit image.")
    path = Path(path)
    fd, temporary_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(b"\x89PNG\r\n\x1a\n")
            _chunk(stream, b"IHDR", struct.pack(">IIBBBBB", width, height, 16 if depth else 8,
                                                0 if depth else 2, 0, 0, 0))
            compressor = zlib.compressobj(level=1)
            source = memoryview(raw)
            pixels = width * height
            for y in range(height):
                if depth:
                    row = array("H")
                    row.frombytes(source[y * width * 2:(y + 1) * width * 2])
                    # Input is always little endian, PNG always big endian.
                    row.byteswap()
                    scanline = row.tobytes()
                else:
                    row = bytearray(width * 3)
                    for output, channel in enumerate((2, 1, 0)):
                        start = channel * pixels + y * width
                        row[output::3] = source[start:start + width]
                    scanline = row
                compressed = compressor.compress(b"\0" + scanline)
                if compressed:
                    _chunk(stream, b"IDAT", compressed)
            _chunk(stream, b"IDAT", compressor.flush())
            _chunk(stream, b"IEND", b"")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
