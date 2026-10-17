"""Scenario files: loading, trigger matching and plan expansion (${VAR} first, then repeat/{n})."""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from copperline_common.config import load_yaml
from copperline_common.personas import Personas

STEP_KINDS = ("token", "model", "tool", "http")
DEFAULT_SCENARIOS_DIR = "/app/scenarios"

# Keys that control expansion and are removed from PlanStep.spec.
_EXPANSION_KEYS = ("repeat", "concurrency")

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
# {n} or {n:<format_spec>}; nothing else in braces is touched.
_N_PATTERN = re.compile(r"\{n(?::([^{}]*))?\}")


class PlanError(Exception):
    """A plan that cannot run: bad step shape or an unresolved ${VAR}."""


@dataclass
class PlanStep:
    i: int  # plan index AFTER repeat expansion (0-based)
    kind: str  # "token" | "model" | "tool" | "http"
    spec: dict  # source step with ${VAR} and {n} substituted (repeat/concurrency removed)
    n: int | None  # 1-based repeat counter, None if not repeated
    group: int  # index of the source step in the YAML plan
    concurrency: int  # from the source step, default 1
    stop_on_status: int | None


@dataclass
class Scenario:
    id: str
    title: str
    persona: str
    agent: str
    message: str
    trigger: str
    plan: list[dict] = field(default_factory=list)
    final_reply: str | None = None
    expect: dict = field(default_factory=dict)
    repeat_message: int | None = None
    source: str = ""

    def matches(self, *, agent_id: str, message: str, sub: str, personas: Personas) -> bool:
        if self.agent != agent_id or personas.base_sub(sub) != self.persona:
            return False
        return re.search(self.trigger, message, re.IGNORECASE) is not None


# ---------------------------------------------------------------------------------------------
# Loading and matching
# ---------------------------------------------------------------------------------------------


def _scenario_from_dict(data: dict, source: str) -> Scenario:
    return Scenario(
        id=str(data["id"]),
        title=str(data.get("title") or ""),
        persona=str(data.get("persona") or ""),
        agent=str(data.get("agent") or ""),
        message=str(data.get("message") or ""),
        trigger=str(data.get("trigger") or ""),
        plan=list(data.get("plan") or []),
        final_reply=data.get("final_reply"),
        expect=dict(data.get("expect") or {}),
        repeat_message=data.get("repeat_message"),
        source=source,
    )


def _scenarios_in_file(path: Path) -> list[Scenario]:
    data = load_yaml(path) or {}
    variants = data.get("variants")
    if not variants:
        return [_scenario_from_dict(data, path.name)]
    file_level = {key: value for key, value in data.items() if key != "variants"}
    return [_scenario_from_dict({**file_level, **variant}, path.name) for variant in variants]


def load_scenarios(directory: str | Path | None = None) -> list[Scenario]:
    """Every *.yaml in the directory, sorted by filename. A file with `variants` gives one Scenario each."""
    folder = Path(directory or os.environ.get("SCENARIOS_DIR") or DEFAULT_SCENARIOS_DIR)
    scenarios: list[Scenario] = []
    for path in sorted(folder.glob("*.yaml")):
        scenarios.extend(_scenarios_in_file(path))
    return scenarios


def find_scenario(
    scenarios: list[Scenario],
    *,
    agent_id: str,
    message: str,
    sub: str,
    personas: Personas,
    hint: str | None = None,
) -> Scenario | None:
    """First scenario (in load order) that matches. With a hint, only that scenario id may match."""
    for scenario in scenarios:
        if hint is not None and scenario.id != hint:
            continue
        if scenario.matches(agent_id=agent_id, message=message, sub=sub, personas=personas):
            return scenario
    return None


def by_id(scenarios: list[Scenario], scenario_id: str) -> Scenario | None:
    for scenario in scenarios:
        if scenario.id == scenario_id:
            return scenario
    return None


# ---------------------------------------------------------------------------------------------
# Substitution and expansion
# ---------------------------------------------------------------------------------------------


def substitute_env(value: Any, env: Mapping[str, str]) -> Any:
    """Replace ${NAME} recursively. A missing or empty NAME raises PlanError."""
    if isinstance(value, str):
        return _ENV_PATTERN.sub(lambda match: _env_value(match.group(1), env), value)
    if isinstance(value, dict):
        return {key: substitute_env(item, env) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute_env(item, env) for item in value]
    return value


def _env_value(name: str, env: Mapping[str, str]) -> str:
    value = env.get(name)
    if not value:
        raise PlanError(f"unresolved ${{{name}}}")
    return value


def substitute_n(value: Any, n: int) -> Any:
    """Replace only {n} and {n:<format_spec>} (e.g. {n:03d}) recursively. Other braces are left alone.

    A format spec that cannot format an int (e.g. {n:03s}) raises PlanError.
    """
    if isinstance(value, str):
        return _N_PATTERN.sub(lambda match: _format_n(n, match.group(1) or ""), value)
    if isinstance(value, dict):
        return {key: substitute_n(item, n) for key, item in value.items()}
    if isinstance(value, list):
        return [substitute_n(item, n) for item in value]
    return value


def _format_n(n: int, spec: str) -> str:
    try:
        return format(n, spec)
    except ValueError as e:
        raise PlanError(f"bad {{n}} format spec {spec!r}: {e}") from e


def _step_kind(step: Any, group: int) -> str:
    if not isinstance(step, dict):
        raise PlanError(f"plan step {group} is not a mapping")
    kinds = [kind for kind in STEP_KINDS if kind in step]
    if len(kinds) != 1:
        raise PlanError(f"plan step {group} must have exactly one of {', '.join(STEP_KINDS)}; found {kinds or 'none'}")
    return kinds[0]


def _positive_int(step: dict, key: str, default: int, group: int) -> int:
    value = step.get(key, default)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise PlanError(f"plan step {group}: {key} must be a positive integer, got {value!r}")
    return value


def _repeated_spec(spec: dict, n: int) -> dict:
    """One copy of a repeated step: args_template becomes args, then {n} is substituted everywhere."""
    spec = dict(spec)
    if "args_template" in spec:
        spec["args"] = spec.pop("args_template")
    return substitute_n(spec, n)


def expand_plan(plan: list[dict], env: Mapping[str, str]) -> list[PlanStep]:
    """Validate and expand a plan. Raises PlanError before anything runs if the plan is bad."""
    steps: list[PlanStep] = []
    for group, source in enumerate(plan or []):
        kind = _step_kind(source, group)
        resolved = substitute_env(source, env)
        repeat = _positive_int(resolved, "repeat", 1, group) if "repeat" in resolved else None
        concurrency = _positive_int(resolved, "concurrency", 1, group)
        stop_on_status = resolved.get("stop_on_status")
        spec = {key: value for key, value in resolved.items() if key not in _EXPANSION_KEYS}
        if repeat is None:
            if "args_template" in spec and "args" not in spec:
                spec["args"] = spec.pop("args_template")
            steps.append(PlanStep(len(steps), kind, spec, None, group, concurrency, stop_on_status))
            continue
        for n in range(1, repeat + 1):
            steps.append(PlanStep(len(steps), kind, _repeated_spec(spec, n), n, group, concurrency, stop_on_status))
    return steps
