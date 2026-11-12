#!/usr/bin/env python3
"""Read Linux process/cgroup memory while an operator performs dashboard scans.

This program never contacts the gateway, camera or robot. It records process
names, not command lines or environment variables. Use inside the same container
as the picker; process 1 is normally the launcher when /start.sh is the entrypoint.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import time


def read_text(path: Path) -> str | None:
    try:
        return path.read_text()
    except (OSError, UnicodeError):
        return None


def numeric(path: Path) -> int | str | None:
    value = read_text(path)
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return value.strip()


def counters(path: Path) -> dict[str, int] | None:
    text = read_text(path)
    if text is None:
        return None
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) == 2:
            try:
                result[fields[0]] = int(fields[1])
            except ValueError:
                pass
    return result


def process_stat(proc: Path, pid: int) -> dict | None:
    text = read_text(proc / str(pid) / "stat")
    if not text:
        return None
    # The comm field may contain spaces and parentheses; fields after its last
    # closing parenthesis begin with Linux stat field 3 (state).
    left, right = text.find("("), text.rfind(")")
    fields = text[right + 1:].split()
    try:
        return {
            "pid": pid, "name": text[left + 1:right], "ppid": int(fields[1]),
            "start_ticks": int(fields[19]), "cpu_ticks": int(fields[11]) + int(fields[12]),
        }
    except (ValueError, IndexError):
        return None


def process_memory(proc: Path, identity: dict) -> dict | None:
    directory = proc / str(identity["pid"])
    status = read_text(directory / "status")
    if status is None:
        return None
    values = {}
    for line in status.splitlines():
        key, _, raw = line.partition(":")
        fields = raw.split()
        if fields and key in {"VmRSS", "VmHWM", "Threads"}:
            try:
                values[key] = int(fields[0]) * (1024 if key != "Threads" else 1)
            except ValueError:
                pass
    rollup = read_text(directory / "smaps_rollup")
    proportional = None
    private = None
    if rollup:
        memory = {}
        for line in rollup.splitlines():
            key, _, raw = line.partition(":")
            fields = raw.split()
            if fields and key in {"Pss", "Private_Clean", "Private_Dirty", "Private_Hugetlb"}:
                try:
                    memory[key] = int(fields[0]) * 1024
                except ValueError:
                    pass
        proportional = memory.get("Pss")
        if "Private_Clean" in memory and "Private_Dirty" in memory:
            private = sum(memory.get(key, 0) for key in ("Private_Clean", "Private_Dirty", "Private_Hugetlb"))
    # Avoid attributing a reused PID's memory to a previous process instance.
    final = process_stat(proc, identity["pid"])
    if final is None or final["start_ticks"] != identity["start_ticks"]:
        return None
    return {
        **identity, "rss_bytes": values.get("VmRSS"), "peak_rss_bytes": values.get("VmHWM"),
        "pss_bytes": proportional, "private_bytes": private, "threads": values.get("Threads"),
    }


def process_tree(proc: Path, root_pid: int, root_start: int) -> tuple[list[dict], bool]:
    identities = {}
    for path in proc.iterdir():
        if path.name.isdigit():
            identity = process_stat(proc, int(path.name))
            if identity:
                identities[identity["pid"]] = identity
    root = identities.get(root_pid)
    if root is None or root["start_ticks"] != root_start:
        return [], False
    selected = {root_pid}
    for _ in range(32):
        children = {pid for pid, item in identities.items() if item["ppid"] in selected}
        newer = selected | children
        if newer == selected:
            break
        selected = newer
    if len(selected) > 32:
        raise RuntimeError("More than 32 descendant processes; use the picker process PID")
    return [memory for pid in sorted(selected) if (memory := process_memory(proc, identities[pid]))], True


def unescape_mount(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match[1], 8)), value)


def discover_cgroup(proc: Path, pid: int) -> tuple[Path, Path, int] | None:
    memberships = read_text(proc / str(pid) / "cgroup") or ""
    mounts = read_text(proc / "self/mountinfo") or ""
    groups = {}
    for line in memberships.splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        if fields[0] == "0" and not fields[1]:
            groups[2] = fields[2]
        elif "memory" in fields[1].split(","):
            groups[1] = fields[2]
    candidates = []
    for line in mounts.splitlines():
        before, separator, after = line.partition(" - ")
        lhs, rhs = before.split(), after.split()
        if not separator or len(lhs) < 5 or len(rhs) < 3:
            continue
        version = 2 if rhs[0] == "cgroup2" else 1 if rhs[0] == "cgroup" and "memory" in rhs[2].split(",") else None
        if version not in groups:
            continue
        mount_root = Path(unescape_mount(lhs[3]))
        mount_point = Path(unescape_mount(lhs[4]))
        group = Path(groups[version])
        try:
            relative = group.relative_to(mount_root)
        except ValueError:
            # A private cgroup namespace may report '/' while mountinfo retains
            # the host's subtree root. Only this exact root case is unambiguous.
            if group != Path("/"):
                continue
            relative = Path(".")
        candidate = (mount_point / relative).resolve()
        if not candidate.is_relative_to(mount_point.resolve()):
            continue
        if (candidate / ("memory.current" if version == 2 else "memory.usage_in_bytes")).is_file():
            candidates.append((candidate, mount_point.resolve(), version))
    return candidates[0] if candidates else None


def finite_limit(value: int | str | None) -> int | None:
    # v1 often reports a huge page-aligned LONG_MAX as 'unlimited'. Preserve its
    # raw value in the report, but never treat it as usable device capacity.
    return value if isinstance(value, int) and 0 < value < 2**60 else None


def cgroup_memory(directory: Path, version: int) -> dict:
    if version == 2:
        result = {
            "current_bytes": numeric(directory / "memory.current"),
            "max_bytes": numeric(directory / "memory.max"),
            "peak_bytes": numeric(directory / "memory.peak"),
            "events": counters(directory / "memory.events"),
        }
    else:
        failcnt = numeric(directory / "memory.failcnt")
        result = {
            "current_bytes": numeric(directory / "memory.usage_in_bytes"),
            "max_bytes": numeric(directory / "memory.limit_in_bytes"),
            "peak_bytes": numeric(directory / "memory.max_usage_in_bytes"),
            "events": {"failcnt": failcnt, **(counters(directory / "memory.oom_control") or {})},
        }
    result.update({"path": str(directory), "version": version})
    limit = finite_limit(result["max_bytes"])
    current = result["current_bytes"]
    result["headroom_bytes"] = limit - current if limit is not None and isinstance(current, int) else None
    return result


def cgroup_hierarchy(location: tuple[Path, Path, int] | None) -> list[dict]:
    if location is None:
        return []
    directory, mount_point, version = location
    result = []
    for _ in range(32):
        result.append(cgroup_memory(directory, version))
        if directory == mount_point:
            break
        directory = directory.parent
    return result


def complete_sum(items: list[dict], key: str) -> int | None:
    return sum(item[key] for item in items) if items and all(isinstance(item[key], int) for item in items) else None


def summarize(samples: list[dict]) -> dict:
    def maximum(values):
        return max((value for value in values if isinstance(value, int)), default=None)

    cgroups = [sample["cgroups"][0] for sample in samples if sample["cgroups"]]
    deltas = {}
    if cgroups and isinstance(cgroups[0]["events"], dict) and isinstance(cgroups[-1]["events"], dict):
        for key, last in cgroups[-1]["events"].items():
            first = cgroups[0]["events"].get(key)
            if isinstance(first, int) and isinstance(last, int):
                deltas[key] = last - first
    return {
        "sample_count": len(samples),
        "peak_sampled_process_pss_bytes": maximum(sample["process_pss_sum_bytes"] for sample in samples),
        "peak_sampled_process_rss_sum_bytes": maximum(sample["process_rss_sum_bytes"] for sample in samples),
        "peak_sampled_cgroup_current_bytes": maximum(item["current_bytes"] for item in cgroups),
        "kernel_cgroup_peak_bytes": maximum(item["peak_bytes"] for item in cgroups),
        "cgroup_event_changes": deltas,
        "visible_cgroup_limits": [{"path": item["path"], "max_bytes": item["max_bytes"]} for item in samples[-1]["cgroups"]] if samples else [],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True, help="Combined gateway/coordinator PID in this namespace")
    parser.add_argument("--samples", type=int, default=60)
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds, default 1")
    parser.add_argument("--output", type=Path, required=True, help="New JSON file, preferably on the persistent SD mount")
    parser.add_argument("--cgroup-dir", type=Path, help="Override automatic discovery when WeOS hides cgroup membership")
    args = parser.parse_args()
    if not sys.platform.startswith("linux"):
        parser.error("This profiler requires Linux /proc; run it inside the Lynx application container")
    if args.pid <= 0 or not 1 <= args.samples <= 3600 or not 0.2 <= args.interval <= 60:
        parser.error("Use a positive PID, 1..3600 samples and an interval from 0.2 to 60 seconds")
    if args.output.exists():
        parser.error("Output already exists; choose a new report filename")
    if not args.output.parent.is_dir():
        parser.error("Output directory does not exist; use the mounted persistent state directory")
    proc = Path("/proc")
    root = process_stat(proc, args.pid)
    if root is None:
        parser.error("The selected process is not readable in this PID namespace")
    location = discover_cgroup(proc, args.pid)
    if args.cgroup_dir:
        directory = args.cgroup_dir.resolve()
        version = 2 if (directory / "memory.current").is_file() else 1 if (directory / "memory.usage_in_bytes").is_file() else None
        if version is None:
            parser.error("The selected cgroup directory exposes no readable memory usage file")
        location = (directory, directory, version)
    samples = []
    started = time.monotonic()
    interrupted = False
    print(f"Sampling PID {args.pid} and descendants; perform scans in the dashboard now.", file=sys.stderr)
    try:
        for index in range(args.samples):
            if index:
                time.sleep(max(0.0, started + index * args.interval - time.monotonic()))
            processes, alive = process_tree(proc, args.pid, root["start_ticks"])
            samples.append({
                "elapsed_seconds": round(time.monotonic() - started, 6),
                "utc": datetime.now(timezone.utc).isoformat(), "root_alive": alive,
                "processes": processes,
                "process_rss_sum_bytes": complete_sum(processes, "rss_bytes"),
                "process_pss_sum_bytes": complete_sum(processes, "pss_bytes"),
                "cgroups": cgroup_hierarchy(location),
            })
            if not alive:
                break
    except KeyboardInterrupt:
        interrupted = True
    report = {
        "schema_version": 1, "root_pid": args.pid, "clock_ticks_per_second": os.sysconf("SC_CLK_TCK"),
        "requested_samples": args.samples, "interval_seconds": args.interval, "interrupted": interrupted,
        "notes": [
            "RSS sums double-count shared pages; do not use them as total memory consumed.",
            "PSS apportions shared pages; null means a process exited or permission denied smaps_rollup.",
            "Cgroup usage includes all members and charged cache/kernel memory, not only these two processes.",
            "The kernel cgroup peak may predate this recording; sampled current gives this recording's observed peak.",
            "Ancestor limits may be shared with other applications. Hidden parents and WeOS host allocation cannot be inferred.",
            "Missing cgroup counters or an unlimited limit are unavailable information, not zero usage or unlimited device RAM.",
            "No camera, inference or robot requests were sent by this profiler.",
        ],
        "summary": summarize(samples), "samples": samples,
    }
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")
    print(f"Wrote {len(samples)} samples to {args.output}", file=sys.stderr)


if __name__ == "__main__":
    main()
