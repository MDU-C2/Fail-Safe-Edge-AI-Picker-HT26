"""Run the full pick, handover and place for one made-up cup, without the camera.

For trying the motion in RobotStudio: start the gateway against the virtual
controller (GATEWAY_ROBOT_HOST=127.0.0.1, mock camera, development mode),
load the program with rapidctl.py --host 127.0.0.1 upload --gripper-stub,
then run this. Prints how long each step took and the total, so the smooth
and classic plans can be compared.

    python capture_and_detect/sim_handover.py                 # smooth plan (path jobs)
    python capture_and_detect/sim_handover.py --classic       # one stop per waypoint
    python capture_and_detect/sim_handover.py --x 450 --y 50  # another cup position
    python capture_and_detect/sim_handover.py --cups 3        # 3 cups: left picks the next while right places
    python capture_and_detect/sim_handover.py --cups 3 --per-cup   # 3 cups, one plan each (no overlap)
    python capture_and_detect/sim_handover.py --cups 3 --y2 -150   # 2nd and later cups at y -150 (near the
                                                                   # place spot: no overlap for those)
    python capture_and_detect/sim_handover.py --dry-run       # print the jobs only

Cups are placed on the spots of placing_grid.json in order (the file is only
read, never marked occupied). The smooth plan uses the real-run poses, up to
flange z 690: start the gateway with GATEWAY_RWS_SAFE_ZONE_MAX=[800,500,700].

Refuses any gateway that is not on this PC unless --allow-remote is given:
the real YuMi must not be driven by a made-up cup.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from handover_plan import handover_batch, handover_jobs, handover_paths  # noqa: E402
from robot_client import RobotClient  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8000/api/v1", help="Gateway API base URL")
    parser.add_argument("--client", default="operator", help="Client id (development mode)")
    parser.add_argument("--token", help="Bearer token instead (strict mode)")
    parser.add_argument("--x", type=float, default=500, help="Cup base x, robot mm (default 500)")
    parser.add_argument("--y", type=float, default=100, help="Cup base y, robot mm (default 100)")
    parser.add_argument("--z", type=float, default=50, help="Cup base z, robot mm (default 50)")
    parser.add_argument("--speed", type=float, default=50, help="Speed near the cup, mm/s (default 50)")
    parser.add_argument("--cups", type=int, default=1, help="Cups in a row (same made-up position, default 1)")
    parser.add_argument("--y2", type=float, help="y of the 2nd and later cups (default: same as --y)")
    parser.add_argument("--per-cup", action="store_true",
                        help="One plan per cup (left waits between cups) instead of one overlapping plan")
    parser.add_argument("--classic", action="store_true", help="Original plan: one stop per waypoint")
    parser.add_argument("--dry-run", action="store_true", help="Validate and print the jobs, send nothing")
    parser.add_argument("--allow-remote", action="store_true", help="Allow a gateway that is not on this PC")
    args = parser.parse_args()

    host = urlparse(args.url).hostname
    if host not in ("127.0.0.1", "localhost") and not args.allow_remote and not args.dry_run:
        parser.error(f"{host} is not this PC. This script is for RobotStudio; use --allow-remote "
                     "only if you know that gateway points at a virtual controller.")

    if args.cups < 1:
        parser.error("--cups must be at least 1")

    def cup_at(y):
        return {"x": args.x, "y": y, "z": args.z, "selectable": True, "blocked_reason": None}

    cups = [cup_at(args.y)] + [cup_at(args.y if args.y2 is None else args.y2)] * (args.cups - 1)
    grid = json.loads((Path(__file__).resolve().parent / "placing_grid.json").read_text())["locations"]
    places = [(spot["x"], spot["y"], spot["z"]) for spot in grid]
    places = [places[number % len(places)] for number in range(len(cups))]
    headers = ({"Authorization": f"Bearer {args.token}"} if args.token
               else {"X-Dev-Client": args.client})
    robot = RobotClient(args.url, headers, dry_run=args.dry_run)

    if args.classic:
        plans = [("classic", handover_jobs(cup, speed=args.speed)) for cup in cups]
    elif args.per_cup or len(cups) == 1:
        plans = [("smooth, " + ("left arm waits after" if number < len(cups) else "left arm homes after"),
                  handover_paths(cup, speed=args.speed, more_cups=number < len(cups),
                                 place=places[number - 1]))
                 for number, cup in enumerate(cups, 1)]
    else:
        plans = [("smooth, all cups in one plan: left picks the next while right places",
                  handover_batch(cups, speed=args.speed, places=places))]

    totals = []
    for number, (kind, jobs) in enumerate(plans, 1):
        started = {}
        durations = []

        def before(index, job):
            started[index] = time.monotonic()
            return None

        def after(index, job):
            durations.append((job["name"], time.monotonic() - started[index]))

        print(f"Plan {number}/{len(plans)}: {kind}, {len(jobs)} jobs")
        total = time.monotonic()
        robot.execute_jobs(jobs, before_send=before, after_done=after)
        if args.dry_run:
            continue
        total = time.monotonic() - total
        totals.append(total)

        # Steps that ran at the same time overlap, so they add up to more than the total.
        print()
        for name, seconds in durations:
            print(f"  {name:22} {seconds:6.2f} s")
        print(f"  {'total':22} {total:6.2f} s")
        print()

    if totals:
        print(f"{len(cups)} cup(s): {sum(totals):.2f} s, {sum(totals) / len(cups):.2f} s per cup")


if __name__ == "__main__":
    main()
