"""Linux memory-controller fixtures; no live /proc, camera or model process."""
import ctypes
import fcntl
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from lynx import resources


MIB = resources.MIB


@pytest.fixture
def startup_clock(monkeypatch):
    clock = SimpleNamespace(now=100.0, sleeps=[], on_sleep=lambda: None)
    def sleep(duration):
        assert 0 < duration <= 0.2
        clock.sleeps.append(duration)
        clock.now += duration
        clock.on_sleep()
    monkeypatch.setattr(resources, "time", SimpleNamespace(monotonic=lambda: clock.now, sleep=sleep))
    return clock


def test_startup_memory_wait_passes_first_measurement_without_sleep(kernel, startup_clock):
    write(kernel.shared / "memory.current", 128 * MIB)
    result = resources.wait_for_model_headroom(128, proc=kernel.proc)
    assert result.cgroup_headroom_bytes == 128 * MIB
    assert startup_clock.sleeps == []


def test_startup_memory_wait_remeasures_shortfall_until_exact_threshold(kernel, startup_clock):
    write(kernel.shared / "memory.current", 140 * MIB)
    startup_clock.on_sleep = lambda: write(kernel.shared / "memory.current", 128 * MIB)
    result = resources.wait_for_model_headroom(128, proc=kernel.proc)
    assert result.cgroup_headroom_bytes == 128 * MIB
    assert startup_clock.sleeps == [0.2]


def test_startup_memory_wait_uses_absolute_deadline_and_preserves_shortfall(kernel, startup_clock):
    write(kernel.shared / "memory.current", 140 * MIB)
    with pytest.raises(resources.InsufficientHeadroomError) as failure:
        resources.wait_for_model_headroom(128, proc=kernel.proc, timeout_s=0.5)
    assert "128 MiB additional memory headroom" in str(failure.value)
    assert "116.0 MiB remaining" in str(failure.value)
    assert "Startup wait of 0.5 seconds expired" in str(failure.value)
    assert startup_clock.now == pytest.approx(100.5)
    assert startup_clock.sleeps == pytest.approx([0.2, 0.2, 0.1])


def test_startup_memory_wait_unknown_counter_fails_immediately(kernel, startup_clock):
    write(kernel.shared / "memory.current", "unavailable")
    with pytest.raises(resources.MemoryGuardError) as failure:
        resources.wait_for_model_headroom(128, proc=kernel.proc)
    assert not isinstance(failure.value, resources.InsufficientHeadroomError)
    assert "could not be verified" in str(failure.value)
    assert startup_clock.sleeps == []


def test_startup_memory_wait_interruption_is_not_retried(kernel, startup_clock):
    write(kernel.shared / "memory.current", 140 * MIB)
    def interrupt():
        raise KeyboardInterrupt
    startup_clock.on_sleep = interrupt
    with pytest.raises(KeyboardInterrupt):
        resources.wait_for_model_headroom(128, proc=kernel.proc)
    assert startup_clock.sleeps == [0.2]


@pytest.mark.parametrize("options", [{"timeout_s": 0}, {"timeout_s": 5.1},
                                    {"timeout_s": float("nan")}, {"timeout_s": True},
                                    {"interval_s": 0}, {"interval_s": 0.21}])
def test_startup_memory_wait_rejects_unbounded_or_invalid_options(kernel, startup_clock, options):
    with pytest.raises(ValueError):
        resources.wait_for_model_headroom(128, proc=kernel.proc, **options)
    assert not startup_clock.sleeps


def test_startup_memory_wait_keeps_existing_requirement_validation(kernel, startup_clock):
    with pytest.raises(ValueError, match="32..128"):
        resources.wait_for_model_headroom(129, proc=kernel.proc)
    assert not startup_clock.sleeps


@pytest.fixture
def python_code_cache(tmp_path, monkeypatch):
    root = (tmp_path / "app").resolve()
    root.mkdir()
    modules, calls, descriptors = {}, [], []
    monkeypatch.setattr(resources, "PYTHON_CODE_ROOTS", (root,))
    monkeypatch.setattr(resources, "sys", SimpleNamespace(platform="linux", modules=modules))
    monkeypatch.setattr(os, "POSIX_FADV_DONTNEED", 4, raising=False)

    def advise(fd, offset, length, advice):
        assert (offset, length, advice) == (0, 0, 4)
        assert fcntl.fcntl(fd, fcntl.F_GETFL) & os.O_ACCMODE == os.O_RDONLY
        info = os.fstat(fd)
        calls.append((info.st_ino, info.st_size))
        descriptors.append(fd)

    monkeypatch.setattr(os, "posix_fadvise", advise, raising=False)
    return SimpleNamespace(root=root, modules=modules, calls=calls, descriptors=descriptors)


def test_source_cache_advice_only_loaded_source_and_bytecode_readonly(python_code_cache):
    rig = python_code_cache
    source, cached, unloaded = rig.root / "module.py", rig.root / "module.pyc", rig.root / "unloaded.py"
    source.write_bytes(b"answer = 42\n")
    cached.write_bytes(b"offline cached code")
    unloaded.write_bytes(b"not imported")
    rig.modules.update(one=SimpleNamespace(__file__=str(source), __cached__=str(cached)),
                       duplicate=SimpleNamespace(__file__=str(source)))
    result = resources.advise_loaded_python_cache()
    assert result.supported and result.files_advised == 2
    assert result.file_bytes_advised == source.stat().st_size + cached.stat().st_size
    assert {inode for inode, _ in rig.calls} == {source.stat().st_ino, cached.stat().st_ino}
    assert source.read_bytes() == b"answer = 42\n" and cached.read_bytes() == b"offline cached code"
    assert "not reclaimed-memory measurements" in result.summary()
    for descriptor in rig.descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_source_cache_excludes_libraries_models_state_gateway_and_outside_roots(python_code_cache):
    rig = python_code_cache
    paths = [rig.root / name for name in ("native.so", "model.ncnn.bin", "placing-grid.json",
                                         "gateway-token", "state/private.py", "gateway/control.py")]
    paths += [rig.root.parent / "outside.py", Path(str(rig.root) + "-other") / "outside.py"]
    for index, path in enumerate(paths):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"must not be advised")
        rig.modules[str(index)] = SimpleNamespace(__file__=str(path))
    rig.modules["traversal"] = SimpleNamespace(__file__=str(rig.root / ".." / "outside.py"))
    rig.modules["relative"] = SimpleNamespace(__file__="relative.py")
    result = resources.advise_loaded_python_cache()
    assert result.files_advised == 0 and rig.calls == []


def test_source_cache_never_follows_links_or_advises_nonregular_files(python_code_cache):
    rig = python_code_cache
    real_directory = rig.root / "real"
    real_directory.mkdir()
    source = real_directory / "module.py"
    source.write_bytes(b"real code")
    file_link, directory_link = rig.root / "file.py", rig.root / "linked"
    file_link.symlink_to(source)
    directory_link.symlink_to(real_directory, target_is_directory=True)
    directory = rig.root / "directory.py"
    directory.mkdir()
    fifo = rig.root / "pipe.py"
    os.mkfifo(fifo)
    for index, path in enumerate((file_link, directory_link / "module.py", directory, fifo)):
        rig.modules[str(index)] = SimpleNamespace(__file__=str(path))
    result = resources.advise_loaded_python_cache()
    assert result.files_advised == 0 and not rig.calls


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_source_cache_advice_unsupported_platform_is_nonfatal(python_code_cache, monkeypatch, platform):
    monkeypatch.setattr(resources.sys, "platform", platform)
    result = resources.advise_loaded_python_cache()
    assert not result.supported and not python_code_cache.calls
    assert "checking measured memory" in result.summary()


def test_source_cache_advice_missing_api_is_nonfatal(python_code_cache, monkeypatch):
    monkeypatch.delattr(os, "posix_fadvise")
    result = resources.advise_loaded_python_cache()
    assert not result.supported and not python_code_cache.calls


def test_source_cache_advice_oserror_keeps_later_files_and_closes_descriptors(python_code_cache, monkeypatch):
    rig = python_code_cache
    paths = [rig.root / "first.py", rig.root / "second.py"]
    for index, path in enumerate(paths):
        path.write_bytes(b"loaded code")
        rig.modules[str(index)] = SimpleNamespace(__file__=str(path))
    descriptors = []
    def advise(fd, *_args):
        descriptors.append(fd)
        if os.fstat(fd).st_ino == paths[0].stat().st_ino:
            raise OSError("offline filesystem does not support advice")
    monkeypatch.setattr(os, "posix_fadvise", advise)
    result = resources.advise_loaded_python_cache()
    assert result.files_advised == 1 and result.failed_files == 1
    assert result.file_bytes_advised == paths[1].stat().st_size
    for descriptor in descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_native_package_cache_has_exact_fixed_deployed_allowlist():
    assert resources._NATIVE_CACHE_FILES == (
        (Path("/usr/local/bin/lynx-ncnn-worker"), 64 * MIB),
        (Path("/opt/model/model.ncnn.param"), MIB),
        (Path("/opt/model/model.ncnn.bin"), 128 * MIB),
    )


@pytest.fixture
def native_package_cache(python_code_cache, monkeypatch):
    rig = python_code_cache
    rig.files = [rig.root / name for name in ("lynx-ncnn-worker", "model.ncnn.param", "model.ncnn.bin")]
    for path in rig.files:
        path.write_bytes((path.name + " verified immutable bytes").encode())
    monkeypatch.setattr(resources, "_NATIVE_CACHE_FILES", tuple((path, MIB) for path in rig.files))
    return rig


def test_native_package_cache_only_exact_readonly_files_without_sync(native_package_cache, monkeypatch):
    rig = native_package_cache
    expected = {path: path.read_bytes() for path in rig.files}
    for name in ("libc.so", "libpython.so", "gateway-token", "placing-grid.json", "manifest.json"):
        (rig.root / name).write_bytes(b"must not be advised")
    def forbid_sync(*_args):
        raise AssertionError("Immutable native files must not be synced")
    monkeypatch.setattr(os, "fsync", forbid_sync)
    result = resources.advise_packaged_native_cache()
    assert result.supported and result.files_advised == 3 and result.failed_files == 0
    assert result.file_bytes_advised == sum(len(value) for value in expected.values())
    assert {inode for inode, _ in rig.calls} == {path.stat().st_ino for path in rig.files}
    assert {path: path.read_bytes() for path in rig.files} == expected
    assert "not reclaimed-memory measurements" in result.summary()
    for descriptor in rig.descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


@pytest.mark.parametrize("replacement", ["symlink", "hardlink", "directory", "fifo", "oversized", "missing"])
def test_native_package_cache_rejects_nonowned_or_unbounded_files(native_package_cache, replacement):
    rig = native_package_cache
    path = rig.files[0]
    path.unlink()
    if replacement == "symlink":
        path.symlink_to(rig.files[1])
    elif replacement == "hardlink":
        os.link(rig.files[1], path)
    elif replacement == "directory":
        path.mkdir()
    elif replacement == "fifo":
        os.mkfifo(path)
    elif replacement == "oversized":
        with path.open("wb") as stream:
            stream.truncate(MIB + 1)
    result = resources.advise_packaged_native_cache()
    # A hardlink also disqualifies its other name, which shares that inode.
    assert result.files_advised == (1 if replacement == "hardlink" else 2)
    if replacement != "missing":
        assert all(inode != path.lstat().st_ino for inode, _ in rig.calls)


def test_native_package_cache_failures_are_nonfatal(native_package_cache, monkeypatch):
    rig = native_package_cache
    def unavailable(*_args):
        raise OSError("offline advice unavailable")
    monkeypatch.setattr(os, "posix_fadvise", unavailable)
    result = resources.advise_packaged_native_cache()
    assert result.files_advised == 0 and result.failed_files == 3
    monkeypatch.setattr(resources.sys, "platform", "darwin")
    assert not resources.advise_packaged_native_cache().supported
    monkeypatch.setattr(resources.sys, "platform", "linux")
    monkeypatch.delattr(os, "posix_fadvise")
    assert not resources.advise_packaged_native_cache().supported


def test_capture_cache_syncs_owned_images_without_touching_records(python_code_cache, monkeypatch):
    rig = python_code_cache
    captures = rig.root / "captures"
    captures.mkdir()
    names = ("rgb_20261116_scan.png", "depth_20261116_verify.png", "old.jpg",
             "old.jpeg", "old.npy", "old.npz", "old.raw", "old.bin")
    files = [captures / name for name in names]
    ignored = [captures / name for name in ("cups.json", "robot_recovery.json",
                                          "grips.csv", "gateway-token", ".private.png")]
    nested = captures / "saved-cups" / "reference.png"
    nested.parent.mkdir()
    ignored.append(nested)
    for path in files + ignored:
        path.write_bytes((path.name + " preserved").encode())
    expected = {path: path.read_bytes() for path in files + ignored}
    synced = []
    original_advise = os.posix_fadvise
    monkeypatch.setattr(os, "fsync", lambda fd: synced.append(os.fstat(fd).st_ino))
    def advise(fd, *args):
        assert synced[-1] == os.fstat(fd).st_ino
        original_advise(fd, *args)
    monkeypatch.setattr(os, "posix_fadvise", advise)
    result = resources.advise_capture_cache(captures, sync=True)
    assert result.supported and result.files_advised == len(files)
    assert result.file_bytes_advised == sum(len(expected[path]) for path in files)
    assert result.failed_files == 0 and not result.limit_reached
    assert set(synced) == {path.stat().st_ino for path in files}
    assert {path: path.read_bytes() for path in files + ignored} == expected
    assert "not reclaimed-memory measurements" in result.summary()
    for descriptor in rig.descriptors:
        with pytest.raises(OSError):
            os.fstat(descriptor)


def test_capture_cache_exact_paths_deduplicated_without_sync_or_enumeration(python_code_cache, monkeypatch):
    rig = python_code_cache
    first, second = rig.root / "rgb.png", rig.root / "depth.png"
    first.write_bytes(b"rgb")
    second.write_bytes(b"depth")
    def forbid_sync(*_args):
        raise AssertionError("Clean image should not be synced again")
    monkeypatch.setattr(os, "fsync", forbid_sync)
    result = resources.advise_capture_cache(rig.root, paths=(str(first), first))
    assert result.files_advised == 1 and result.file_bytes_advised == 3
    assert rig.calls == [(first.stat().st_ino, 3)]


def test_capture_cache_rejects_links_special_files_and_paths_outside_root(python_code_cache):
    rig = python_code_cache
    real = rig.root / "original.png"
    real.write_bytes(b"preserve original")
    link = rig.root / "alias.png"
    link.symlink_to(real)
    hardlink = rig.root / "hardlink.png"
    os.link(real, hardlink)
    fifo = rig.root / "pipe.png"
    os.mkfifo(fifo)
    directory = rig.root / "directory.png"
    directory.mkdir()
    outside = rig.root.parent / "outside.png"
    outside.write_bytes(b"not owned")
    result = resources.advise_capture_cache(rig.root)
    assert result.files_advised == 0 and not rig.calls
    result = resources.advise_capture_cache(rig.root, paths=(outside, rig.root / ".." / "outside.png"))
    assert result.failed_files == 2 and not rig.calls
    alias = rig.root.parent / "linked-captures"
    alias.symlink_to(rig.root, target_is_directory=True)
    result = resources.advise_capture_cache(alias)
    assert result.files_advised == 0 and result.failed_files == 1
    nested = directory / "test.png"
    nested.write_bytes(b"nested")
    result = resources.advise_capture_cache(rig.root, paths=(nested,))
    assert result.failed_files == 1 and not rig.calls


def test_capture_cache_failure_is_nonfatal_and_other_files_still_advised(python_code_cache, monkeypatch):
    rig = python_code_cache
    first, second = rig.root / "a.png", rig.root / "b.png"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    def sync(fd):
        if os.fstat(fd).st_ino == first.stat().st_ino:
            raise OSError("synthetic flush failure")
    monkeypatch.setattr(os, "fsync", sync)
    result = resources.advise_capture_cache(rig.root, sync=True)
    assert result.files_advised == 1 and result.failed_files == 1
    assert rig.calls == [(second.stat().st_ino, 6)]
    monkeypatch.setattr(os, "posix_fadvise", lambda *_args: (_ for _ in ()).throw(OSError("unavailable")))
    result = resources.advise_capture_cache(rig.root)
    assert result.files_advised == 0 and result.failed_files == 2
    assert first.read_bytes() == b"first" and second.read_bytes() == b"second"


@pytest.mark.parametrize("bound,value", [("_CAPTURE_MAX_FILES", 2), ("_CAPTURE_MAX_BYTES", 8),
                                        ("_CAPTURE_MAX_ENTRIES", 2)])
def test_capture_cache_bounds_work_and_never_claims_every_file_advised(python_code_cache, monkeypatch, bound, value):
    rig = python_code_cache
    for index in range(4):
        (rig.root / f"rgb_{index}.png").write_bytes(b"four")
    monkeypatch.setattr(resources, bound, value)
    result = resources.advise_capture_cache(rig.root)
    assert result.files_advised == 2 and result.file_bytes_advised == 8
    assert result.limit_reached and "Work limit reached" in result.summary()


def test_capture_cache_skips_large_or_empty_files(python_code_cache, monkeypatch):
    rig = python_code_cache
    (rig.root / "empty.png").touch()
    (rig.root / "large.png").write_bytes(b"too large")
    (rig.root / "small.png").write_bytes(b"ok")
    monkeypatch.setattr(resources, "_CAPTURE_MAX_FILE_BYTES", 2)
    result = resources.advise_capture_cache(rig.root)
    assert result.files_advised == 1 and result.file_bytes_advised == 2


@pytest.mark.parametrize("root,options", [("relative", {}), (None, {}),
                                        ("ROOT", {"paths": ["x"] * 17}),
                                        ("ROOT", {"paths": iter(["x"])}),
                                        ("ROOT", {"paths": [None]}),
                                        ("ROOT", {"sync": "yes"})])
def test_capture_cache_invalid_inputs_are_nonfatal(python_code_cache, root, options):
    result = resources.advise_capture_cache(python_code_cache.root if root == "ROOT" else root, **options)
    assert result.files_advised == 0 and result.failed_files == 1
    assert not python_code_cache.calls


def test_capture_cache_unsupported_platform_or_api_is_nonfatal(python_code_cache, monkeypatch):
    monkeypatch.setattr(resources.sys, "platform", "darwin")
    assert not resources.advise_capture_cache(python_code_cache.root).supported
    monkeypatch.setattr(resources.sys, "platform", "linux")
    monkeypatch.delattr(os, "posix_fadvise")
    assert not resources.advise_capture_cache(python_code_cache.root).supported
    assert not python_code_cache.calls


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
