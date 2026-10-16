"""Demo personas and clients from personas.yaml, including run-salt handling."""

from dataclasses import dataclass, field
from pathlib import Path

from copperline_common.config import config_file, load_yaml


@dataclass
class Personas:
    personas: list[dict] = field(default_factory=list)
    clients: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Personas":
        data = load_yaml(path or config_file("personas.yaml")) or {}
        return cls(personas=list(data.get("personas") or []), clients=list(data.get("clients") or []))

    def get(self, sub: str) -> dict | None:
        """Exact sub match on the base personas (no salt stripping)."""
        for persona in self.personas:
            if persona.get("sub") == sub:
                return persona
        return None

    def base_sub(self, sub: str) -> str:
        """'mallory.x-ab12' -> 'mallory.x', but only if 'mallory.x' is a salted persona."""
        base, dash, _salt = sub.rpartition("-")
        if not dash or not base:
            return sub
        persona = self.get(base)
        if persona is not None and persona.get("salted"):
            return base
        return sub

    def resolve(self, sub: str) -> dict | None:
        return self.get(self.base_sub(sub))

    def customer_id(self, sub: str) -> str | None:
        persona = self.resolve(sub)
        return persona.get("customer_id") if persona else None

    def client(self, client_id: str) -> dict | None:
        for client in self.clients:
            if client.get("client_id") == client_id:
                return client
        return None


def salted_sub(sub: str, run_salt: str | None) -> str:
    return f"{sub}-{run_salt}" if run_salt else sub
