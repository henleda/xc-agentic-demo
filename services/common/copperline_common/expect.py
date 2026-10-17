"""Check a trace against a scenario `expect` block (semantics: scenarios/README.md, "Assertion semantics")."""

from dataclasses import dataclass

MATCH_KEYS = ("status", "blocked_by", "is_error")
BODY_KEYS = ("body_not_contains", "body_not_contains_alt")
HARD_SECTIONS = ("steps", "estate_delta", "front_door")


@dataclass
class Check:
    ok: bool
    description: str
    detail: str


@dataclass
class ExpectResult:
    ok: bool
    checks: list[Check]
    skipped: bool  # block has no steps / estate_delta / front_door (missing, empty, or soft-only); ok is then False


def select_block(expect: dict | None, env: str) -> dict | None:
    return (expect or {}).get(env)


# ---------------------------------------------------------------------------------------------
# Matching one trace step against one spec
# ---------------------------------------------------------------------------------------------


def _wanted(spec: dict) -> str:
    parts = [f"{key}={spec[key]}" for key in MATCH_KEYS if key in spec]
    return " ".join(parts) or "present"


def _observed(step: dict) -> str:
    text = f"status={step.get('status')} blocked_by={step.get('blocked_by')}"
    if step.get("is_error") is not None:
        text += f" is_error={step.get('is_error')}"
    if step.get("reason"):
        text += f" reason={step.get('reason')}"
    return text


def _field_mismatch(step: dict, spec: dict) -> str | None:
    """None when the step matches status/blocked_by/is_error, else what differs."""
    for key in MATCH_KEYS:
        if key in spec and step.get(key) != spec[key]:
            return f"{key}: expected {spec[key]}, got {step.get(key)}"
    if spec.get("status") == 200 and step.get("kind") == "tool":
        # A tool only succeeded if an MCP result came back with isError false. A 200 with no parsed
        # result (an HTML block page, an empty body, a JSON-RPC error) has is_error None and must fail.
        if step.get("is_error") is True:
            return "status 200 but the tool result has isError: true"
        if step.get("is_error") is not False:
            return "status 200 but no MCP result"
    return None


def _as_list(value) -> list[str]:
    if value is None:
        return []
    return [value] if isinstance(value, str) else [str(item) for item in value]


def _body_problem(step: dict, spec: dict) -> str | None:
    forbidden = [text for key in BODY_KEYS for text in _as_list(spec.get(key))]
    if not forbidden:
        return None
    if step.get("result_truncated"):
        return "result_preview was truncated, so body_not_contains cannot be proven"
    preview = step.get("result_preview") or ""
    found = [text for text in forbidden if text in preview]
    if found:
        return f"body contains {found[0]!r}"
    return None


def _step_problem(step: dict, spec: dict) -> str | None:
    return _field_mismatch(step, spec) or _body_problem(step, spec)


# ---------------------------------------------------------------------------------------------
# Section checks
# ---------------------------------------------------------------------------------------------


def _by_index(trace: list[dict]) -> dict[int, dict]:
    steps: dict[int, dict] = {}
    for step in trace:
        if isinstance(step.get("i"), int) and step.get("kind") != "front_door":
            steps.setdefault(step["i"], step)
    return steps


def _check_single(spec: dict, steps: dict[int, dict]) -> Check:
    index = spec["i"]
    description = f"step i={index}: {_wanted(spec)}"
    step = steps.get(index)
    if step is None:
        return Check(False, description, "step not in trace")
    problem = _step_problem(step, spec)
    return Check(problem is None, description, problem or _observed(step))


def _check_range(spec: dict, steps: dict[int, dict]) -> Check:
    first, last = spec["i_range"]
    in_range = [steps[index] for index in range(first, last + 1) if index in steps]
    matching = [step for step in in_range if _step_problem(step, spec) is None]
    label = f"steps i={first}..{last}: {_wanted(spec)}"

    if "count" in spec:
        tolerance = spec.get("tolerance", 0)
        ok = abs(len(matching) - spec["count"]) <= tolerance
        return Check(ok, f"{label}, count {spec['count']} +/- {tolerance}", f"{len(matching)} matched")
    if "min_count" in spec:
        ok = len(matching) >= spec["min_count"]
        return Check(ok, f"{label}, at least {spec['min_count']}", f"{len(matching)} matched")

    missing = [index for index in range(first, last + 1) if index not in steps]
    if missing:
        return Check(False, f"{label}, every step", f"missing i={missing[:10]}")
    bad = [(step["i"], _step_problem(step, spec)) for step in in_range if _step_problem(step, spec)]
    if bad:
        return Check(False, f"{label}, every step", f"i={bad[0][0]}: {bad[0][1]} ({len(bad)} failing)")
    return Check(True, f"{label}, every step", f"{len(in_range)} matched")


def _check_steps(specs: list[dict], trace: list[dict]) -> list[Check]:
    steps = _by_index(trace)
    checks = []
    for spec in specs:
        if "i" in spec:
            checks.append(_check_single(spec, steps))
        elif "i_range" in spec:
            checks.append(_check_range(spec, steps))
        else:
            checks.append(Check(False, f"step spec {spec}", "needs i or i_range"))
    return checks


def _check_estate(expected: dict, tolerance: dict, observed: dict | None) -> list[Check]:
    if observed is None:
        return [Check(False, f"estate_delta {expected}", "estate not observed")]
    checks = []
    for key, want in expected.items():
        allowed = (tolerance or {}).get(key, 0)
        description = f"estate_delta {key} = {want} +/- {allowed}"
        if key not in observed:
            checks.append(Check(False, description, f"{key} not observed"))
            continue
        got = observed[key]
        ok = isinstance(got, int | float) and abs(got - want) <= allowed
        checks.append(Check(ok, description, f"observed {got}"))
    return checks


def _check_front_door(specs: list[dict], trace: list[dict]) -> list[Check]:
    attempts = {step.get("attempt"): step for step in trace if step.get("kind") == "front_door"}
    checks = []
    for spec in specs:
        description = f"front door attempt {spec.get('attempt')}: {_wanted(spec)}"
        step = attempts.get(spec.get("attempt"))
        if step is None:
            checks.append(Check(False, description, "attempt not in trace"))
            continue
        problem = _field_mismatch(step, spec)
        checks.append(Check(problem is None, description, problem or _observed(step)))
    return checks


def evaluate(block: dict | None, trace: list[dict], *, estate_delta: dict | None = None) -> ExpectResult:
    block = block or {}
    checks: list[Check] = []
    if block.get("steps"):
        checks += _check_steps(block["steps"], trace)
    if block.get("estate_delta"):
        checks += _check_estate(block["estate_delta"], block.get("tolerance") or {}, estate_delta)
    if block.get("front_door"):
        checks += _check_front_door(block["front_door"], trace)
    for text in block.get("soft") or []:
        checks.append(Check(True, f"(soft) {text}", "not asserted"))

    skipped = not any(block.get(section) for section in HARD_SECTIONS)
    if skipped:
        return ExpectResult(ok=False, checks=checks, skipped=True)
    return ExpectResult(ok=all(check.ok for check in checks), checks=checks, skipped=False)
