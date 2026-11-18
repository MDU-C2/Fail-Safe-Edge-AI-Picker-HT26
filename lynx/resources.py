"""Bounded owned-file cache advice and a measured Linux model-startup check.

Cgroup usage includes charged cache: this guard does not assume it can be
reclaimed. A passing snapshot neither reserves memory nor proves later peaks fit.
"""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import sys
import time


MIB = 1024 * 1024


class MemoryGuardError(RuntimeError):
    pass


class InsufficientHeadroomError(MemoryGuardError):
    """Readable memory measurements are below the required startup margin."""


PYTHON_CODE_ROOTS = (Path("/app"), Path("/opt/venv"), Path("/usr/local/lib/python3.11"))


@dataclass(frozen=True)
class PythonCacheAdvice:
    supported: bool
    files_advised: int = 0
    file_bytes_advised: int = 0
    failed_files: int = 0

    def summary(self):
        if not self.supported:
            return "Python source-cache advice unavailable; checking measured memory without it."
        return (f"Python source-cache advice: {self.files_advised} loaded files, "
                f"{self.file_bytes_advised} file bytes; {self.failed_files} files unavailable. "
                "File sizes are not reclaimed-memory measurements; the memory check still applies.")


def _open_python_code(path):
    """Open a code file read-only without following any directory/file link."""
    directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in path.parts[1:-1]:
            next_directory = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                     dir_fd=directory)
            os.close(directory)
            directory = next_directory
        return os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
    finally:
        os.close(directory)


def advise_loaded_python_cache():
    """Best-effort clean page-cache advice for this process's loaded Python code.

    Code objects remain in memory. No file is read, written, removed or synced;
    no library/model/state/gateway file or unloaded module is considered. Kernel
    advice can reclaim nothing, so only the subsequent measured guard may admit
    model startup. The allowlist is fixed to the packaged CLI filesystem.
    """
    if (sys.platform != "linux" or not callable(getattr(os, "posix_fadvise", None))
            or any(not hasattr(os, name) for name in
                   ("POSIX_FADV_DONTNEED", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))
            or os.open not in os.supports_dir_fd):
        return PythonCacheAdvice(False)
    modules = list(sys.modules.values())
    candidates = set()
    for module in modules[:4096]:
        namespace = getattr(module, "__dict__", {})
        for key in ("__file__", "__cached__"):
            value = namespace.get(key)
            if not isinstance(value, str) or len(value) > 4096:
                continue
            path = Path(value)
            if (not path.is_absolute() or path.suffix not in (".py", ".pyc")
                    or any(part in ("..", "state", "gateway") for part in path.parts)
                    or not any(path.is_relative_to(root) for root in PYTHON_CODE_ROOTS)):
                continue
            candidates.add(path)
            if len(candidates) >= 1024:
                break
        if len(candidates) >= 1024:
            break
    count = total = failed = 0
    seen = set()
    for path in sorted(candidates):
        descriptor = None
        try:
            descriptor = _open_python_code(path)
            info = os.fstat(descriptor)
            identity = (info.st_dev, info.st_ino)
            if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 4 * MIB
                    or identity in seen):
                continue
            seen.add(identity)
            os.posix_fadvise(descriptor, 0, 0, os.POSIX_FADV_DONTNEED)
            count += 1
            total += info.st_size
        except (OSError, ValueError):
            failed += 1
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return PythonCacheAdvice(True, count, total, failed)


@dataclass(frozen=True)
class NativeCacheAdvice:
    supported: bool
    files_advised: int = 0
    file_bytes_advised: int = 0
    failed_files: int = 0

    def summary(self):
        if not self.supported:
            return "Native package-cache advice unavailable; checking measured memory without it."
        return (f"Native package-cache advice: {self.files_advised} files, "
                f"{self.file_bytes_advised} file bytes; {self.failed_files} files unavailable. "
                "File sizes are not reclaimed-memory measurements; the memory check still applies.")


# Exact immutable files in the deployed package. No directory walk, configurable
# path, shared library, state file or gateway file can enter this advice set.
_NATIVE_CACHE_FILES = (
    (Path("/usr/local/bin/lynx-ncnn-worker"), 64 * MIB),
    (Path("/opt/model/model.ncnn.param"), MIB),
    (Path("/opt/model/model.ncnn.bin"), 128 * MIB),
)


def advise_packaged_native_cache():
    """Advise clean cache for exactly three packaged worker/model files.

    Called only during native startup, after model checksum verification and
    before the child is spawned. This also releases cache left by an earlier,
    already-reaped worker. No file is read, written, synced, changed or removed;
    a later model load/execution may fault pages back from the preserved files.
    Only the subsequent measured memory guard may admit startup.
    """
    if (sys.platform != "linux" or not callable(getattr(os, "posix_fadvise", None))
            or any(not hasattr(os, name) for name in
                   ("POSIX_FADV_DONTNEED", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))
            or os.open not in os.supports_dir_fd):
        return NativeCacheAdvice(False)
    count = total = failed = 0
    for path, size_limit in _NATIVE_CACHE_FILES:
        descriptor = None
        try:
            descriptor = _open_python_code(path)
            info = os.fstat(descriptor)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or not 0 < info.st_size <= size_limit):
                continue
            os.posix_fadvise(descriptor, 0, 0, os.POSIX_FADV_DONTNEED)
            count += 1
            total += info.st_size
        except (OSError, ValueError):
            failed += 1
        finally:
            if descriptor is not None:
                os.close(descriptor)
    return NativeCacheAdvice(True, count, total, failed)


@dataclass(frozen=True)
class CaptureCacheAdvice:
    supported: bool
    files_advised: int = 0
    file_bytes_advised: int = 0
    failed_files: int = 0
    limit_reached: bool = False

    def summary(self):
        if not self.supported:
            return "Saved-image cache advice unavailable; checking measured memory without it."
        limited = " Work limit reached." if self.limit_reached else ""
        return (f"Saved-image cache advice: {self.files_advised} files, "
                f"{self.file_bytes_advised} file bytes; {self.failed_files} files unavailable. "
                "Files are preserved. File sizes are not reclaimed-memory measurements; "
                "the memory check still applies." + limited)


_CAPTURE_SUFFIXES = frozenset((".png", ".jpg", ".jpeg", ".npy", ".npz", ".raw", ".bin"))
_CAPTURE_MAX_ENTRIES = 4096
_CAPTURE_MAX_FILES = 256
_CAPTURE_MAX_BYTES = 256 * MIB
_CAPTURE_MAX_FILE_BYTES = 24 * MIB


def advise_capture_cache(folder, *, paths=None, sync=False):
    """Best-effort DONTNEED for completed artifacts in one owned capture folder.

    Startup uses ``sync=True`` to flush historical dirty artifacts before advice.
    Newly written PNGs are already fsynced; callers serving those files can use
    the default without another flush. Neither mode changes or removes content.
    Only direct regular, singly linked raster/raw files are considered, never
    JSON/CSV records, credentials, subdirectories, or symlinks at any path level.

    Work is bounded to 4096 directory entries, 256 files, and 256 MiB of file
    sizes (24 MiB per file). ``paths`` optionally narrows advice to at most 16
    exact absolute children, e.g. the two just-written or one just-served images.
    Bounds limit IO volume, not filesystem latency. Advice and sync may reclaim
    nothing; callers must still measure actual memory. Failure is nonfatal.
    """
    if (sys.platform != "linux" or not callable(getattr(os, "posix_fadvise", None))
            or any(not hasattr(os, name) for name in
                   ("POSIX_FADV_DONTNEED", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK"))
            or os.open not in os.supports_dir_fd or os.scandir not in os.supports_fd):
        return CaptureCacheAdvice(False)
    directory = None
    count = total = failed = attempts = attempted_bytes = 0
    limited = False
    try:
        root = Path(folder)
        if (not root.is_absolute() or ".." in root.parts or len(str(root)) > 4096
                or type(sync) is not bool):
            return CaptureCacheAdvice(True, failed_files=1)
        names = None
        if paths is not None:
            if not isinstance(paths, (tuple, list)) or len(paths) > 16:
                return CaptureCacheAdvice(True, failed_files=1)
            names = set()
            for value in paths:
                path = Path(value)
                if path.parent != root or ".." in path.parts or len(str(path)) > 4096:
                    failed += 1
                else:
                    names.add(path.name)
        # Reuse the component-by-component no-follow opener; verify this last
        # component is a directory before enumerating/opening anything below it.
        directory = _open_python_code(root)
        if not stat.S_ISDIR(os.fstat(directory).st_mode):
            return CaptureCacheAdvice(True, failed_files=failed + 1)
        if names is None:
            names = set()
            with os.scandir(directory) as entries:
                for index, entry in enumerate(entries):
                    if index >= _CAPTURE_MAX_ENTRIES:
                        limited = True
                        break
                    names.add(entry.name)
        for name in sorted(names):
            if (not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,199}", name)
                    or Path(name).suffix.lower() not in _CAPTURE_SUFFIXES):
                continue
            descriptor = None
            try:
                descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                     dir_fd=directory)
                info = os.fstat(descriptor)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or not 0 < info.st_size <= _CAPTURE_MAX_FILE_BYTES):
                    continue
                if (attempts >= _CAPTURE_MAX_FILES
                        or attempted_bytes + info.st_size > _CAPTURE_MAX_BYTES):
                    limited = True
                    break
                attempts += 1
                attempted_bytes += info.st_size
                if sync:
                    os.fsync(descriptor)
                os.posix_fadvise(descriptor, 0, 0, os.POSIX_FADV_DONTNEED)
                count += 1
                total += info.st_size
            except (OSError, ValueError):
                failed += 1
            finally:
                if descriptor is not None:
                    os.close(descriptor)
    except (OSError, ValueError, TypeError):
        failed += 1
    finally:
        if directory is not None:
            os.close(directory)
    return CaptureCacheAdvice(True, count, total, failed, limited)


def trim_unused_heap():
    """Best-effort glibc trim; its return value is not a byte measurement."""
    try:
        import gc
        import ctypes
        import os
        # Retired controller/session callbacks may form cycles. Collect these
        # before asking glibc to return unused arenas; no byte saving is assumed.
        gc.collect()
        version = os.confstr("CS_GNU_LIBC_VERSION")
        if not version or not version.startswith("glibc "):
            return "Heap trim unavailable on this C library; checking memory without it."
        trim = ctypes.CDLL(None).malloc_trim
        trim.argtypes = [ctypes.c_size_t]
        trim.restype = ctypes.c_int
        released = trim(0)
    except (ImportError, AttributeError, OSError, ValueError):
        return "Heap trim unavailable; checking memory without it."
    result = "released pages" if released else "no pages released"
    return f"glibc heap trim attempted; allocator reported {result}. No freed-byte amount is assumed."


def _read(path):
    try:
        with path.open() as stream:
            text = stream.read(128 * 1024 + 1)
        return text.strip() if len(text) <= 128 * 1024 else None
    except (OSError, UnicodeError):
        return None


def _missing(path):
    try:
        path.stat()
    except FileNotFoundError:
        return True
    except OSError:
        pass
    return False


def _number(text):
    if text is None or not re.fullmatch(r"[0-9]+", text):
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _mount_path(text):
    path = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), text))
    return path if path.is_absolute() and ".." not in path.parts else None


@dataclass(frozen=True)
class CgroupMemory:
    path: Path
    limit_bytes: int | None
    current_bytes: int | None
    limit_kind: str  # finite, unlimited or unavailable

    @property
    def headroom_bytes(self):
        if self.limit_bytes is None or self.current_bytes is None:
            return None
        return self.limit_bytes - self.current_bytes


@dataclass(frozen=True)
class MemorySnapshot:
    cgroups: tuple[CgroupMemory, ...]
    host_available_bytes: int | None
    hidden_ancestors: bool
    problems: tuple[str, ...]

    @property
    def cgroup_headroom_bytes(self):
        return min((g.headroom_bytes for g in self.cgroups if g.headroom_bytes is not None), default=None)

    def summary(self, required_mib):
        # Only called after the check has established both measurements.
        hidden = " Parent limits outside this mount remain unknown." if self.hidden_ancestors else ""
        return (f"Model startup memory check: {self.cgroup_headroom_bytes / MIB:.1f} MiB visible cgroup headroom; "
                f"{self.host_available_bytes / MIB:.1f} MiB host available; {required_mib} MiB required. "
                "This snapshot does not reserve memory or verify later peaks." + hidden)


def _location(proc):
    """Choose the matching mount exposing the widest visible hierarchy."""
    groups = {}
    for line in (_read(proc / "self/cgroup") or "").splitlines():
        fields = line.split(":", 2)
        if len(fields) == 3:
            if fields[0] == "0" and not fields[1]:
                groups[2] = _mount_path(fields[2])
            elif "memory" in fields[1].split(","):
                groups[1] = _mount_path(fields[2])
    # On a hybrid system a memory controller assigned to v1 is not in v2.
    version = 1 if groups.get(1) is not None else 2
    group = groups.get(version)
    if group is None:
        return None
    candidates = []
    for line in (_read(proc / "self/mountinfo") or "").splitlines():
        before, separator, after = line.partition(" - ")
        lhs, rhs = before.split(), after.split()
        if not separator or len(lhs) < 5 or len(rhs) < 3:
            continue
        if not ((version == 2 and rhs[0] == "cgroup2") or
                (version == 1 and rhs[0] == "cgroup" and "memory" in rhs[2].split(","))):
            continue
        root, mount = _mount_path(lhs[3]), _mount_path(lhs[4])
        if root is None or mount is None:
            continue
        try:
            relative = group.relative_to(root)
        except ValueError:
            # Private cgroup namespaces may report '/' for a subtree mount.
            if group != Path("/"):
                continue
            relative = Path(".")
        directory, mount = (mount / relative).resolve(), mount.resolve()
        if directory.is_relative_to(mount):
            candidates.append((len(root.parts), directory, mount, version, root != Path("/")))
    if not candidates:
        return None
    _, directory, mount, version, hidden = min(candidates, key=lambda item: item[0])
    return directory, mount, version, hidden


def memory_snapshot(proc=Path("/proc")):
    proc = Path(proc)
    host_available = None
    for line in (_read(proc / "meminfo") or "").splitlines():
        fields = line.split()
        if len(fields) == 3 and fields[0] == "MemAvailable:" and fields[2] == "kB":
            value = _number(fields[1])
            host_available = None if value is None else value * 1024
            break
    groups, problems = [], []
    location = _location(proc)
    if location is None:
        return MemorySnapshot((), host_available, True, ("the memory cgroup could not be located",))
    directory, mount, version, hidden = location
    if version != 2:
        # A v1 root has memory files even at the global root, so the mount alone
        # cannot establish that a namespace is not hiding a shared ancestor.
        return MemorySnapshot((), host_available, True, ("cgroup v1 ancestor visibility cannot be verified by this startup guard",))
    for _ in range(64):
        limit_path = directory / "memory.max"
        current_path = directory / "memory.current"
        limit_text = _read(limit_path)
        current = _number(_read(current_path))
        # The real v2 root has no memory limit/current files. A subtree mount is
        # not that root and must not silently receive this exemption.
        true_root = directory == mount and not hidden
        if not (true_root and _missing(limit_path) and _missing(current_path)):
            if directory == mount:
                # A namespace can report root='/' while mounting a non-root
                # cgroup. Its memory controller files reveal that distinction.
                hidden = True
            limit = _number(limit_text)
            unlimited = limit_text == "max"
            kind = "unlimited" if unlimited else "finite" if limit is not None else "unavailable"
            if unlimited:
                limit = None
            groups.append(CgroupMemory(directory, limit, current, kind))
            if kind == "unavailable":
                problems.append(f"the memory limit at {directory} is unavailable")
            elif kind == "finite" and current is None:
                problems.append(f"memory usage at {directory} is unavailable")
        if directory == mount:
            break
        directory = directory.parent
    else:
        problems.append("the visible memory cgroup hierarchy exceeds 64 levels")
    return MemorySnapshot(tuple(groups), host_available, hidden, tuple(problems))


def require_model_headroom(required_mib=48, *, proc=Path("/proc")):
    """Refuse model startup on insufficient or unmeasurable memory headroom."""
    if type(required_mib) is not int or not 32 <= required_mib <= 128:
        raise ValueError("Model startup headroom must be 32..128 MiB.")
    snapshot = memory_snapshot(proc)
    unknown = list(snapshot.problems)
    if snapshot.hidden_ancestors:
        unknown.append("ancestor memory limits are hidden by the cgroup mount or namespace")
    if snapshot.host_available_bytes is None:
        unknown.append("host MemAvailable is unavailable")
    if snapshot.cgroup_headroom_bytes is None:
        unknown.append("no finite readable cgroup limit is visible; parent limits may be hidden")
    if unknown:
        raise MemoryGuardError("Native model was not started because memory headroom could not be verified: "
                               + "; ".join(unknown) + ". Expose the hosting memory counters before restarting.")
    required = required_mib * MIB
    short = [f"cgroup {g.path} has {g.headroom_bytes / MIB:.1f} MiB remaining"
             for g in snapshot.cgroups if g.headroom_bytes is not None and g.headroom_bytes < required]
    if snapshot.host_available_bytes < required:
        short.append(f"host MemAvailable is {snapshot.host_available_bytes / MIB:.1f} MiB")
    if short:
        raise InsufficientHeadroomError(f"Native model was not started: at least {required_mib} MiB additional memory headroom is required; "
                                        + "; ".join(short) + ". Reduce resource use and restart. Charged cache is not assumed reclaimable.")
    return snapshot


def wait_for_model_headroom(required_mib=128, *, proc=Path("/proc"), timeout_s=5.0,
                            interval_s=0.2):
    """Wait briefly for measured transient pressure; never relax the guard.

    Unknown hierarchy/counters and interruptions propagate immediately. Only a
    measured shortfall is retried, with one absolute deadline and no worker.
    """
    if (type(timeout_s) not in (int, float) or not 0 < timeout_s <= 5
            or type(interval_s) not in (int, float) or not 0 < interval_s <= 0.2):
        raise ValueError("Startup memory wait must be 0..5 seconds and polling 0..0.2 seconds, both positive.")
    deadline = time.monotonic() + timeout_s
    last_shortfall = None
    while True:
        try:
            snapshot = require_model_headroom(required_mib, proc=proc)
        except InsufficientHeadroomError as error:
            last_shortfall = error
        else:
            if time.monotonic() <= deadline:
                return snapshot
            # A slow measurement cannot extend the bounded startup wait.
            if last_shortfall is None:
                raise MemoryGuardError("Native model was not started: the memory check exceeded the startup deadline.")
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(interval_s, remaining))
        if time.monotonic() >= deadline:
            raise InsufficientHeadroomError(
                f"{last_shortfall} Startup wait of {timeout_s:g} seconds expired; no model was started."
            ) from last_shortfall
