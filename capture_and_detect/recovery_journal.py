"""Persistent recovery acknowledgement, independent of camera scan files."""

import json
from pathlib import Path

from cup_inventory import atomic_json


def _reject_constant(value):
    raise ValueError(f"Invalid JSON number in recovery state: {value}")


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"Duplicate JSON field in recovery state: {key}")
        value[key] = item
    return value


def _read_json(path):
    with path.open(encoding="utf-8") as stream:
        return json.load(stream, parse_constant=_reject_constant, object_pairs_hook=_unique_object)


class RecoveryJournal:
    """Keep motion locked after a crash until completion or explicit recovery.

    Absence of a journal permits the legacy inventory migration check. A present
    but unreadable or malformed file is never treated as a cleared journal.
    """

    def __init__(self, path, legacy_inventory_path=None):
        self.path = Path(path)
        self.legacy_inventory_path = (Path(legacy_inventory_path)
                                      if legacy_inventory_path is not None else None)

    def required(self):
        try:
            state = _read_json(self.path)
        except FileNotFoundError:
            required = self._legacy_required()
            if required:
                # A later scan replaces cups.json. Preserve the legacy lockout
                # now so that replacing it and restarting cannot erase evidence.
                self.begin({"source": "legacy_inventory",
                            "legacy_inventory_path": str(self.legacy_inventory_path)})
            return required
        if (not isinstance(state, dict) or type(state.get("schema_version")) is not int
                or state["schema_version"] != 1
                or type(state.get("recovery_required")) is not bool
                or ("context" in state and not isinstance(state["context"], dict))):
            raise ValueError(f"Invalid recovery journal: {self.path}")
        return state["recovery_required"]

    def _legacy_required(self):
        if self.legacy_inventory_path is None:
            return False
        try:
            inventory = _read_json(self.legacy_inventory_path)
        except FileNotFoundError:
            return False
        cups = inventory.get("cups") if isinstance(inventory, dict) else inventory
        if not isinstance(cups, list):
            raise ValueError(f"Invalid legacy cup inventory: {self.legacy_inventory_path}")
        required = False
        for cup in cups:
            if not isinstance(cup, dict):
                raise ValueError("Invalid cup entry in legacy recovery state.")
            status = cup.get("status")
            if status is not None and not isinstance(status, str):
                raise ValueError("Invalid cup status in legacy recovery state.")
            required |= status in ("movement_unconfirmed", "command_pending")
            if "execution" in cup:
                execution = cup["execution"]
                if (not isinstance(execution, dict)
                        or execution.get("state") not in ("pending", "running", "interrupted", "completed")):
                    raise ValueError("Invalid execution state in legacy recovery state.")
                required |= execution["state"] in ("pending", "running", "interrupted")
        return required

    def begin(self, context):
        if not isinstance(context, dict):
            raise ValueError("Recovery context must be an object.")
        atomic_json(self.path, {"schema_version": 1, "recovery_required": True,
                                "context": context})

    def clear(self):
        atomic_json(self.path, {"schema_version": 1, "recovery_required": False})
