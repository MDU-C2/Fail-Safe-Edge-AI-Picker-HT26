import importlib.util
from pathlib import Path
import tempfile
import unittest


spec = importlib.util.spec_from_file_location("measure_resources", Path(__file__).with_name("measure_resources.py"))
measure = importlib.util.module_from_spec(spec)
spec.loader.exec_module(measure)


class ResourcesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.proc = self.root / "proc"
        self.proc.mkdir()

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def process(self, pid, ppid, name, start=100, pss=12):
        fields = ["0"] * 50
        fields[0], fields[1], fields[11], fields[12], fields[19] = "S", str(ppid), "10", "3", str(start)
        self.write(self.proc / str(pid) / "stat", f"{pid} ({name}) " + " ".join(fields))
        self.write(self.proc / str(pid) / "status", "VmRSS:\t20 kB\nVmHWM:\t30 kB\nThreads:\t2\n")
        if pss is not None:
            self.write(self.proc / str(pid) / "smaps_rollup", f"Pss: {pss} kB\nPrivate_Clean: 2 kB\nPrivate_Dirty: 3 kB\n")

    def test_tree_accounts_for_shared_memory_without_claiming_rss_total(self):
        self.process(1, 0, "python (picker)")
        self.process(2, 1, "lynx-ncnn", pss=8)
        self.process(3, 0, "unrelated")
        processes, alive = measure.process_tree(self.proc, 1, 100)
        self.assertTrue(alive)
        self.assertEqual([item["pid"] for item in processes], [1, 2])
        self.assertEqual(measure.complete_sum(processes, "rss_bytes"), 40 * 1024)
        self.assertEqual(measure.complete_sum(processes, "pss_bytes"), 20 * 1024)
        self.assertEqual(processes[0]["private_bytes"], 5 * 1024)
        self.assertEqual(processes[0]["cpu_ticks"], 13)
        self.assertEqual(measure.process_tree(self.proc, 1, 99), ([], False))

    def test_missing_pss_is_unavailable_not_zero(self):
        self.process(1, 0, "picker", pss=None)
        processes, _ = measure.process_tree(self.proc, 1, 100)
        self.assertIsNone(measure.complete_sum(processes, "pss_bytes"))
        self.assertEqual(measure.complete_sum(processes, "rss_bytes"), 20 * 1024)

    def test_v2_discovery_limits_and_events(self):
        mount = self.root / "cgroup"
        group = mount / "app"
        self.write(group / "memory.current", "100\n")
        self.write(group / "memory.max", "1000\n")
        self.write(group / "memory.peak", "200\n")
        self.write(group / "memory.events", "high 1\noom 0\noom_kill 0\n")
        self.write(mount / "memory.current", "250\n")
        self.write(mount / "memory.max", "max\n")
        self.write(self.proc / "1/cgroup", "0::/app\n")
        self.write(self.proc / "self/mountinfo", f"20 10 0:25 / {mount} rw - cgroup2 cgroup rw\n")
        location = measure.discover_cgroup(self.proc, 1)
        self.assertEqual(location, (group, mount, 2))
        hierarchy = measure.cgroup_hierarchy(location)
        self.assertEqual(hierarchy[0]["headroom_bytes"], 900)
        self.assertEqual(hierarchy[0]["events"]["high"], 1)
        self.assertEqual(hierarchy[1]["max_bytes"], "max")
        self.assertIsNone(hierarchy[1]["headroom_bytes"])

    def test_v1_discovery_and_unlimited_sentinel(self):
        mount = self.root / "memory"
        self.write(mount / "memory.usage_in_bytes", "100\n")
        self.write(mount / "memory.limit_in_bytes", "9223372036854771712\n")
        self.write(mount / "memory.max_usage_in_bytes", "200\n")
        self.write(mount / "memory.failcnt", "2\n")
        self.write(mount / "memory.oom_control", "oom_kill_disable 0\nunder_oom 0\noom_kill 1\n")
        self.write(self.proc / "1/cgroup", "5:memory:/docker/picker\n")
        self.write(self.proc / "self/mountinfo", f"20 10 0:25 /docker/picker {mount} rw - cgroup cgroup rw,memory\n")
        location = measure.discover_cgroup(self.proc, 1)
        self.assertEqual(location, (mount, mount, 1))
        item = measure.cgroup_hierarchy(location)[0]
        self.assertEqual(item["events"]["failcnt"], 2)
        self.assertEqual(item["events"]["oom_kill"], 1)
        self.assertIsNone(item["headroom_bytes"])


if __name__ == "__main__":
    unittest.main()
