"""Linux memory-controller fixtures; no live /proc, camera or model process."""
import ctypes
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from lynx import resources


MIB = resources.MIB


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(value) + "\n")


@pytest.fixture
def kernel(tmp_path):
    proc, mount = tmp_path / "proc", tmp_path / "cgroup"
    shared, leaf = mount / "apps", mount / "apps/lynxpicker"
    write(proc / "self/cgroup", "0::/apps/lynxpicker")
    write(proc / "self/mountinfo", f"20 10 0:25 / {mount} ro - cgroup2 cgroup ro")
    write(proc / "meminfo", "MemTotal: 496652 kB\nMemAvailable: 204800 kB")
    write(leaf / "memory.max", "max")
    write(leaf / "memory.current", 100 * MIB)
    write(shared / "memory.max", 256 * MIB)
    write(shared / "memory.current", 192 * MIB)
    return SimpleNamespace(proc=proc, mount=mount, shared=shared, leaf=leaf)


def test_unlimited_leaf_uses_shared_ancestor_and_true_v2_root(kernel):
    snapshot = resources.require_model_headroom(proc=kernel.proc)
    assert snapshot.host_available_bytes == 200 * MIB
    assert snapshot.cgroup_headroom_bytes == 64 * MIB
    assert [g.path for g in snapshot.cgroups] == [kernel.leaf, kernel.shared]
    assert snapshot.cgroups[0].limit_kind == "unlimited"
    assert snapshot.cgroups[0].headroom_bytes is None
    assert not snapshot.hidden_ancestors
    assert "does not reserve memory" in snapshot.summary(48)


def test_tighter_parent_blocks_despite_room_in_leaf(kernel):
    write(kernel.leaf / "memory.max", 256 * MIB)
    write(kernel.shared / "memory.current", 240 * MIB)
    with pytest.raises(resources.MemoryGuardError, match=r"cgroup .*apps has 16.0 MiB"):
        resources.require_model_headroom(proc=kernel.proc)


@pytest.mark.parametrize("remaining,allowed", [(48 * MIB, True), (48 * MIB - 1, False), (-1, False)])
def test_exact_boundary_and_over_limit_usage(kernel, remaining, allowed):
    write(kernel.shared / "memory.current", 256 * MIB - remaining)
    if allowed:
        assert resources.require_model_headroom(proc=kernel.proc).cgroup_headroom_bytes == remaining
    else:
        with pytest.raises(resources.MemoryGuardError, match="additional memory headroom"):
            resources.require_model_headroom(proc=kernel.proc)


def test_zero_limit_is_insufficient_not_unlimited(kernel):
    write(kernel.leaf / "memory.max", 0)
    write(kernel.leaf / "memory.current", 0)
    with pytest.raises(resources.MemoryGuardError, match="0.0 MiB remaining"):
        resources.require_model_headroom(proc=kernel.proc)


@pytest.mark.parametrize("value", [None, "-1", "not-a-number"])
def test_finite_limit_with_unreadable_usage_is_unknown(kernel, value):
    path = kernel.shared / "memory.current"
    path.unlink() if value is None else write(path, value)
    with pytest.raises(resources.MemoryGuardError, match="memory usage .* unavailable"):
        resources.require_model_headroom(proc=kernel.proc)


def test_missing_nonroot_limit_is_unknown_even_with_finite_parent(kernel):
    (kernel.leaf / "memory.max").unlink()
    with pytest.raises(resources.MemoryGuardError, match="memory limit .* unavailable"):
        resources.require_model_headroom(proc=kernel.proc)


def test_all_visible_limits_unlimited_do_not_imply_device_capacity(kernel):
    write(kernel.shared / "memory.max", "max")
    with pytest.raises(resources.MemoryGuardError, match="no finite readable cgroup limit"):
        resources.require_model_headroom(proc=kernel.proc)


@pytest.mark.parametrize("raw", ["MemAvailable: -1 kB", "MemAvailable: 100 MB", "MemFree: 204800 kB", ""])
def test_missing_or_invalid_memavailable_is_unknown(kernel, raw):
    write(kernel.proc / "meminfo", raw)
    with pytest.raises(resources.MemoryGuardError, match="host MemAvailable is unavailable"):
        resources.require_model_headroom(proc=kernel.proc)


@pytest.mark.parametrize("available_kib", [0, 48 * 1024 - 1])
def test_host_availability_can_be_tighter_than_cgroups(kernel, available_kib):
    write(kernel.proc / "meminfo", f"MemAvailable: {available_kib} kB")
    with pytest.raises(resources.MemoryGuardError, match="host MemAvailable is"):
        resources.require_model_headroom(proc=kernel.proc)


def test_cache_is_not_subtracted_from_charged_cgroup_usage(kernel):
    write(kernel.shared / "memory.current", 240 * MIB)
    write(kernel.shared / "memory.stat", f"file {200 * MIB}\ninactive_file {200 * MIB}")
    with pytest.raises(resources.MemoryGuardError, match="cache is not assumed reclaimable"):
        resources.require_model_headroom(proc=kernel.proc)


@pytest.mark.parametrize("root", ["/apps/lynxpicker", "/"])
@pytest.mark.parametrize("limit", ["max", 256 * MIB])
def test_private_namespace_or_subtree_cannot_hide_parent_limits(kernel, root, limit):
    write(kernel.proc / "self/cgroup", "0::/")
    write(kernel.proc / "self/mountinfo", f"20 10 0:25 {root} {kernel.leaf} ro - cgroup2 cgroup ro")
    write(kernel.leaf / "memory.max", limit)
    with pytest.raises(resources.MemoryGuardError, match="ancestor memory limits are hidden"):
        resources.require_model_headroom(proc=kernel.proc)


def test_widest_mount_is_used_even_when_subtree_mount_appears_first(kernel):
    narrow = kernel.proc.parent / "narrow"
    write(narrow / "memory.current", 10 * MIB)
    write(narrow / "memory.max", 256 * MIB)
    write(kernel.proc / "self/mountinfo", f"21 10 0:25 /apps/lynxpicker {narrow} ro - cgroup2 cgroup ro\n"
          f"20 10 0:25 / {kernel.mount} ro - cgroup2 cgroup ro")
    write(kernel.shared / "memory.current", 240 * MIB)
    with pytest.raises(resources.MemoryGuardError, match=r"cgroup .*apps has 16.0 MiB"):
        resources.require_model_headroom(proc=kernel.proc)


@pytest.mark.parametrize("membership", ["", "garbage", "0::/../../other"])
def test_unknown_membership_does_not_fall_back_to_host_memory_alone(kernel, membership):
    write(kernel.proc / "self/cgroup", membership)
    with pytest.raises(resources.MemoryGuardError, match="cgroup could not be located"):
        resources.require_model_headroom(proc=kernel.proc)


@pytest.mark.parametrize("membership", ["/", "/apps/lynxpicker"])
def test_v1_refuses_when_global_ancestor_visibility_cannot_be_proven(kernel, membership):
    write(kernel.proc / "self/cgroup", f"5:memory:{membership}\n0::/unrelated")
    write(kernel.proc / "self/mountinfo", f"20 10 0:25 / {kernel.mount} ro - cgroup cgroup ro,memory")
    for path, limit, current in ((kernel.leaf, 9223372036854771712, 100 * MIB),
                                 (kernel.shared, 256 * MIB, 192 * MIB),
                                 (kernel.mount, 9223372036854771712, 300 * MIB)):
        write(path / "memory.limit_in_bytes", limit)
        write(path / "memory.usage_in_bytes", current)
    with pytest.raises(resources.MemoryGuardError, match="cgroup v1 ancestor visibility cannot be verified"):
        resources.require_model_headroom(proc=kernel.proc)


@pytest.mark.parametrize("mib", [31, 129, 48.0, True])
def test_resource_api_rejects_invalid_reserve(kernel, mib):
    with pytest.raises(ValueError, match="32..128"):
        resources.require_model_headroom(mib, proc=kernel.proc)


@pytest.mark.parametrize("result,expected", [(0, "no pages released"), (1, "reported released pages")])
def test_glibc_trim_reports_allocator_result_without_inventing_bytes(monkeypatch, result, expected):
    calls = []

    class Trim:
        def __call__(self, pad):
            calls.append(pad)
            return result

    trim = Trim()
    monkeypatch.setattr(os, "confstr", lambda key: "glibc 2.36")
    monkeypatch.setattr(ctypes, "CDLL", lambda library: SimpleNamespace(malloc_trim=trim))
    message = resources.trim_unused_heap()
    assert calls == [0]
    assert expected in message and "No freed-byte amount is assumed" in message
    assert trim.argtypes == [ctypes.c_size_t] and trim.restype == ctypes.c_int


@pytest.mark.parametrize("libc", [None, "musl 1.2.5"])
def test_other_c_library_skips_optional_trim(monkeypatch, libc):
    monkeypatch.setattr(os, "confstr", lambda key: libc)
    monkeypatch.setattr(ctypes, "CDLL", lambda library: pytest.fail("must not load libc"))
    assert "unavailable" in resources.trim_unused_heap()


def test_missing_glibc_trim_symbol_does_not_block_memory_measurement(monkeypatch):
    monkeypatch.setattr(os, "confstr", lambda key: "glibc 2.36")
    monkeypatch.setattr(ctypes, "CDLL", lambda library: object())
    assert "unavailable" in resources.trim_unused_heap()
