"""Conservative, read-only Linux memory check before loading the native model.

Cgroup usage includes charged cache: this guard does not assume it can be
reclaimed. A passing snapshot neither reserves memory nor proves later peaks fit.
"""
from dataclasses import dataclass
from pathlib import Path
import re


MIB = 1024 * 1024


class MemoryGuardError(RuntimeError):
    pass


def trim_unused_heap():
    """Best-effort glibc trim; its return value is not a byte measurement."""
    try:
        import ctypes
        import os
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
        raise MemoryGuardError(f"Native model was not started: at least {required_mib} MiB additional memory headroom is required; "
                               + "; ".join(short) + ". Reduce resource use and restart. Charged cache is not assumed reclaimable.")
    return snapshot
