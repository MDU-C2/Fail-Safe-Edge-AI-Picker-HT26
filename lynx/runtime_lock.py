"""Process exclusion for native workers sharing one persistent picker state."""

import errno
import os
from pathlib import Path
import stat
import threading


class NativeRuntimeLock:
    """Hold one native runtime until its worker has been closed.

    Acquiring never waits or starts a model. The persistent lock file is not
    removed: unlinking a locked file could let a second process lock a different
    inode. A process exit releases the kernel lock, including after a crash.
    Importing this module remains possible on non-POSIX development machines.
    """

    def __init__(self, state_dir):
        self.path = Path(state_dir).resolve(strict=True) / "picker-native.lock"
        self._descriptor = None
        self._mutex = threading.Lock()

    def acquire(self):
        import fcntl
        with self._mutex:
            if self._descriptor is not None:
                return self
            flags = (os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
                     | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
            descriptor = os.open(self.path, flags, 0o600)
            try:
                opened = os.fstat(descriptor)
                current = os.lstat(self.path)
                if (not stat.S_ISREG(opened.st_mode) or not stat.S_ISREG(current.st_mode)
                        or not os.path.samestat(opened, current)):
                    raise ValueError("Native runtime lock must be a regular state file.")
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError as error:
                    if error.errno in (errno.EACCES, errno.EAGAIN):
                        raise RuntimeError("A native picker already uses this state directory. "
                                           "Close its active controller before starting another runtime.") from None
                    raise
                self._descriptor = descriptor
            except BaseException:
                os.close(descriptor)
                raise
        return self

    def close(self):
        with self._mutex:
            if self._descriptor is not None:
                os.close(self._descriptor)
                self._descriptor = None

    def __enter__(self):
        return self.acquire()

    def __exit__(self, *_args):
        self.close()
