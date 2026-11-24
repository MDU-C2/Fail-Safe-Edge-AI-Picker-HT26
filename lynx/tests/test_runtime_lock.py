"""Kernel-backed native-worker exclusion; no camera or robot is contacted."""
import os
import subprocess
import sys

import pytest

from lynx.runtime_lock import NativeRuntimeLock


def test_competing_native_runtimes_are_excluded_until_close(tmp_path):
    lock = NativeRuntimeLock(tmp_path)
    assert not lock.path.exists()
    lock.acquire()
    with pytest.raises(RuntimeError, match="already uses this state"):
        NativeRuntimeLock(tmp_path).acquire()
    lock.close()
    lock.close()
    with NativeRuntimeLock(tmp_path):
        pass
    assert lock.path.is_file()  # Never unlink and create a separately lockable inode.


def test_context_exception_releases_exclusion(tmp_path):
    with pytest.raises(ValueError, match="startup failed"):
        with NativeRuntimeLock(tmp_path):
            raise ValueError("startup failed")
    with NativeRuntimeLock(tmp_path):
        pass


def test_another_process_cannot_start_while_native_runtime_is_active(tmp_path):
    code = """
import sys
from lynx.runtime_lock import NativeRuntimeLock
try:
    NativeRuntimeLock(sys.argv[1]).acquire()
except RuntimeError:
    raise SystemExit(23)
raise SystemExit(0)
"""
    with NativeRuntimeLock(tmp_path):
        result = subprocess.run([sys.executable, "-c", code, str(tmp_path)],
                                capture_output=True, text=True, timeout=5)
    assert result.returncode == 23, result.stderr
    result = subprocess.run([sys.executable, "-c", code, str(tmp_path)],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    # The child exits without calling close: the kernel releases its lock.
    with NativeRuntimeLock(tmp_path):
        pass


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_native_lock_refuses_nonregular_state_files(tmp_path, kind):
    target = tmp_path / "picker-native.lock"
    if kind == "symlink":
        original = tmp_path / "private-record"
        original.write_text("preserve")
        target.symlink_to(original)
    elif kind == "directory":
        target.mkdir()
    else:
        os.mkfifo(target)
    with pytest.raises((OSError, ValueError)):
        NativeRuntimeLock(tmp_path).acquire()
    if kind == "symlink":
        assert original.read_text() == "preserve"
