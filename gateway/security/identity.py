from dataclasses import dataclass


@dataclass(frozen=True)
class Identity:
    client_id: str
    authenticated: bool
    permissions: frozenset[str]
    control_priority: int

    def can(self, permission: str) -> bool:
        return "*" in self.permissions or permission in self.permissions