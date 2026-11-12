"""
Show or reset placing_grid.json, the place locations used by capture_and_detect.py
and by the web app's cup command (smooth plan).

    python reset_grid.py              # every location free again (after clearing the table)
    python reset_grid.py --show       # only show which locations are occupied
    python reset_grid.py --id 3 7     # free only locations 3 and 7

Only reset while capture_and_detect.py is not moving a cup: during a hand-over it holds
its own copy of the grid and writes it back after placing, which would undo the reset.
(The web app reads the file fresh when it marks a spot, so it does not undo a reset.)
"""

import argparse
import json
from pathlib import Path

PLACE_FILE = Path(__file__).resolve().parent / "placing_grid.json"


def load_places(path=PLACE_FILE):
    with open(path) as f:
        return json.load(f)


def save_places(places, path=PLACE_FILE):
    """Same layout as capture_and_detect.py writes it: one location per line."""
    lines = ",\n".join("    " + json.dumps(p) for p in places["locations"])
    with open(path, "w") as f:
        f.write(f'{{\n  "locations": [\n{lines}\n  ]\n}}\n')


def show(places):
    locations = places["locations"]
    taken = [p["id"] for p in locations if p["occupied"]]
    print(f"{len(locations) - len(taken)} of {len(locations)} locations free"
          + (f"; occupied: {', '.join(map(str, taken))}" if taken else ""))


def main():
    parser = argparse.ArgumentParser(description="Show or reset the place locations in placing_grid.json")
    parser.add_argument("--show", action="store_true", help="only show the grid, change nothing")
    parser.add_argument("--id", type=int, nargs="+", help="free only these location ids")
    args = parser.parse_args()

    places = load_places()

    if args.show:
        show(places)
        return

    ids = {p["id"] for p in places["locations"]}
    unknown = sorted(set(args.id or []) - ids)
    if unknown:
        raise SystemExit(f"No such location id: {', '.join(map(str, unknown))} (ids are {min(ids)}..{max(ids)})")

    for p in places["locations"]:
        if args.id is None or p["id"] in args.id:
            p["occupied"] = False

    save_places(places)
    show(places)


if __name__ == "__main__":
    main()
