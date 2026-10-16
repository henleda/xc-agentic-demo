import pytest
from copperline_common.personas import Personas
from copperline_common.scenarios import (
    PlanError,
    by_id,
    expand_plan,
    find_scenario,
    load_scenarios,
    substitute_env,
    substitute_n,
)


@pytest.fixture(scope="module")
def real_scenarios(repo_root):
    return load_scenarios(repo_root / "scenarios")


@pytest.fixture(scope="module")
def real_personas(repo_root):
    return Personas.load(repo_root / "config/personas.yaml")


def test_substitute_n_formats_and_leaves_other_braces():
    assert substitute_n("S-{n:03d}", 7) == "S-007"
    assert substitute_n("page={n}", 12) == "page=12"
    assert substitute_n('{"x": {y}} {n} {nn} {n!r}', 3) == '{"x": {y}} 3 {nn} {n!r}'
    assert substitute_n({"a": ["{n}", 5, None], "b": "{n:>4}"}, 9) == {"a": ["9", 5, None], "b": "   9"}


def test_substitute_env_recursive_and_errors():
    env = {"BASE": "http://x", "EMPTY": ""}
    assert substitute_env({"u": ["${BASE}/a", "{n}"], "k": 1}, env) == {"u": ["http://x/a", "{n}"], "k": 1}
    with pytest.raises(PlanError, match=r"unresolved \$\{MISSING\}"):
        substitute_env("${MISSING}/x", env)
    with pytest.raises(PlanError, match=r"unresolved \$\{EMPTY\}"):
        substitute_env(["${EMPTY}"], env)


def test_repeat_expansion_is_one_based_with_args_template():
    plan = [
        {"model": {"route": "standard"}},
        {
            "tool": "inventory.adjust_stock",
            "args_template": {"sku": "TNT", "store_id": "S-{n:03d}", "delta": 2},
            "repeat": 40,
            "stop_on_status": None,
        },
    ]
    steps = expand_plan(plan, {})
    assert len(steps) == 41
    first, last = steps[1], steps[40]
    assert (first.i, first.n, first.group, first.kind) == (1, 1, 1, "tool")
    assert first.spec["args"] == {"sku": "TNT", "store_id": "S-001", "delta": 2}
    assert "args_template" not in first.spec and "repeat" not in first.spec
    assert (last.i, last.n) == (40, 40) and last.spec["args"]["store_id"] == "S-040"
    assert steps[0].n is None and steps[0].group == 0 and steps[0].concurrency == 1
    assert [step.i for step in steps] == list(range(41))


def test_env_is_substituted_before_n():
    """${VAR} first: a variable whose VALUE contains {n} is then expanded by the repeat."""
    plan = [{"http": {"method": "GET", "url": "${BASE}/po?page={n}"}, "repeat": 2, "concurrency": 10}]
    steps = expand_plan(plan, {"BASE": "http://p/{n}"})
    assert [step.spec["http"]["url"] for step in steps] == ["http://p/1/po?page=1", "http://p/2/po?page=2"]
    assert all(step.concurrency == 10 and "concurrency" not in step.spec for step in steps)


def test_unresolved_variable_fails_before_anything():
    with pytest.raises(PlanError):
        expand_plan([{"tool": "a.b"}, {"http": {"method": "POST", "url": "${EXFIL_TEST_URL}"}}], {})


@pytest.mark.parametrize(
    "step",
    [{"args": {}}, {"tool": "a.b", "model": {"route": "fast"}}, "tool", {"tool": "a.b", "repeat": 0}],
)
def test_bad_steps(step):
    with pytest.raises(PlanError):
        expand_plan([step], {})


def test_load_real_scenarios(real_scenarios):
    ids = [s.id for s in real_scenarios]
    assert ids == ["S0", "S1", "S1b", "S2", "S3a", "S3b", "S4", "S5", "S6", "S7"]
    s3a = by_id(real_scenarios, "S3a")
    assert s3a.title.startswith("On-behalf-of") and s3a.source == "S3-obo.yaml" and s3a.persona == "sam.patel"
    assert by_id(real_scenarios, "S7").repeat_message == 8
    assert by_id(real_scenarios, "S1").final_reply.startswith("Here's a summary")
    assert by_id(real_scenarios, "S0").final_reply is None
    assert by_id(real_scenarios, "nope") is None


def test_every_real_plan_expands(real_scenarios):
    env = {"EXFIL_TEST_URL": "http://exfil", "TOOLS_BASE_URL": "http://tools", "PARTNER_BASE_URL": "http://partner"}
    sizes = {s.id: len(expand_plan(s.plan, env)) for s in real_scenarios}
    assert sizes == {"S0": 3, "S1": 3, "S1b": 1, "S2": 2, "S3a": 2, "S3b": 2, "S4": 41, "S5": 6, "S6": 54, "S7": 0}


def test_load_scenarios_uses_env_dir(monkeypatch, tmp_path):
    (tmp_path / "b.yaml").write_text("id: B\ntitle: b\npersona: p\nagent: a\nmessage: m\ntrigger: x\nplan: []\n")
    (tmp_path / "a.yaml").write_text("id: A\ntitle: a\npersona: p\nagent: a\nmessage: m\ntrigger: x\nplan: []\n")
    monkeypatch.setenv("SCENARIOS_DIR", str(tmp_path))
    assert [s.id for s in load_scenarios()] == ["A", "B"]


def test_matching_with_salt_and_hint(real_scenarios, real_personas):
    found = find_scenario(real_scenarios, agent_id="shopper", message="Please SUMMARIZE the reviews for the Alpine 2!",
                          sub="mallory.x-run42", personas=real_personas)
    assert found.id == "S1"
    # Right message, wrong human.
    assert find_scenario(real_scenarios, agent_id="shopper", message="summarize the reviews for the alpine 2",
                         sub="alex.rivera", personas=real_personas) is None
    # S3a and S3b share a trigger; the persona picks the variant.
    s3_message = "Refund $120 on CO-1042, item arrived damaged."
    assert find_scenario(real_scenarios, agent_id="refunds", message=s3_message, sub="morgan.chen",
                         personas=real_personas).id == "S3b"
    assert find_scenario(real_scenarios, agent_id="refunds", message=s3_message, sub="sam.patel",
                         personas=real_personas).id == "S3a"
    # A hint narrows to one id; trigger and persona must still match.
    assert find_scenario(real_scenarios, agent_id="refunds", message=s3_message, sub="sam.patel",
                         personas=real_personas, hint="S3b") is None
    assert find_scenario(real_scenarios, agent_id="insights-nightly", message="anything",
                         sub="job:insights-nightly", personas=real_personas).id == "S5"
