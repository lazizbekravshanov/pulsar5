"""End-to-end runs, SAMPLE_MODE isolation, and H-MEM changing a later plan."""

import json
from pathlib import Path

import pytest

from app.config import load_settings
from app.engines.hmem.engine import HMem
from app.orchestrator import Orchestrator
from app.schemas import Event, RunRequest

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "input_examples"


def _request(name: str, **options) -> RunRequest:
    payload = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
    payload["options"] = {**payload.get("options", {}), **options}
    return RunRequest(**payload)


def test_example_1_runs_and_is_fully_traceable():
    response = Orchestrator().run(_request("example_1.json"))

    assert response.status in {"completed", "escalated"}
    assert response.output is not None
    assert response.output.plan.actions
    assert response.zetaris_queries > 0, "every fact must come through Zetaris"
    assert {"world_model", "hmem", "markovian"} <= set(response.engines_used)
    # models_attempted is the one that must always be populated: it is the model
    # named on every request. models_used holds only models that actually served
    # content, so in SAMPLE_MODE with no recorded responses it is correctly empty.
    assert response.models_attempted, "the model behind each call must be logged"
    assert set(response.models_used) <= set(response.models_attempted)
    assert Path(response.log_file).exists()
    assert response.execution_time_seconds < 60

    for action in response.output.plan.actions:
        if action.kind != "hold":
            assert action.evidence_ids, f"{action.action_id} cites no evidence"


def test_three_examples_produce_meaningfully_different_outputs():
    results = {}
    for name in ("example_1.json", "example_2.json", "example_3.json"):
        response = Orchestrator().run(_request(name))
        assert response.output is not None
        results[name] = {
            "actions": len(response.output.plan.actions),
            "evacuees": response.output.simulation.metrics["expected_evacuees"],
            "zones": tuple(sorted(
                a.target for a in response.output.plan.actions if a.kind == "evacuate_zone"
            )),
            "queries": response.zetaris_queries,
        }

    one, two, three = (results[f"example_{i}.json"] for i in (1, 2, 3))

    # A two-county request must not plan the whole five-county region.
    assert two["evacuees"] < one["evacuees"]
    assert two["zones"] != one["zones"]
    assert two["actions"] < one["actions"]

    # The 24-step replay crosses the forecast shift, so it lands somewhere else.
    assert three["zones"] != one["zones"]
    assert three["queries"] > one["queries"] * 5, "a 24-hour replay must query per hour"


def test_sample_mode_needs_no_credentials_and_no_network():
    """A judge with no Zetaris and no Nightshift must still get a real run."""
    settings = load_settings()
    settings.sample_mode = True
    settings.zetaris_rest_url = ""
    settings.zetaris_api_key = ""
    settings.meterless_base_url = ""
    settings.meterless_token = ""

    response = Orchestrator(settings).run(_request("example_1.json", sample_mode=True))

    assert response.status in {"completed", "escalated"}
    assert response.output is not None
    assert response.output.plan.actions, "sample mode must run the real agent logic"
    assert response.output.simulation is not None
    assert response.output.critique is not None
    assert response.iterations >= 1


def test_sample_mode_never_reads_output_examples(monkeypatch):
    """The recorded answers must come from the cache, never from the answers."""
    forbidden = str((REPO / "output_examples").resolve())
    real_open = Path.open

    def guarded(self, *args, **kwargs):
        assert forbidden not in str(self.resolve()), f"sample mode read {self}"
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    response = Orchestrator().run(_request("example_1.json", sample_mode=True))
    assert response.output is not None


def test_hmem_recall_from_an_earlier_run_changes_a_later_plan(tmp_path, monkeypatch):
    """Run 2 must plan differently because of what run 1 learned."""
    store = tmp_path / "store.json"
    monkeypatch.setattr("app.orchestrator.HMem", lambda *a, **k: HMem(store))

    first = Orchestrator().run(_request("example_1.json"))
    assert first.output is not None
    assert first.output.memory_recalled == [], "the first run has nothing to recall"

    # Record what a real run would learn: a shelter that fills faster than its
    # headroom suggests. The orchestrator turns this into a planning derate.
    opened = [a for a in first.output.plan.actions if a.kind == "open_shelter"]
    assert opened
    memory = HMem(store)
    memory.remember(
        f"shelter:{opened[0].target}",
        f"Shelter {opened[0].target} filled hours earlier than its headroom suggested",
        tags=["ian_2022", "lee", "charlotte", "sarasota", "pinellas", "hillsborough"],
        confidence=0.85,
        run_id=first.run_id,
    )
    memory.flush()

    second = Orchestrator().run(_request("example_1.json"))
    assert second.output is not None

    assert second.output.memory_recalled, "run 2 must recall run 1's lesson"
    assert any("filled hours earlier" in m for m in second.output.memory_recalled)
    # The derate reduces usable headroom, so the plan is not identical.
    assert (
        second.output.simulation.metrics["open_shelter_capacity"]
        != first.output.simulation.metrics["open_shelter_capacity"]
    ), "a recalled lesson must change the plan, not just the prompt"


def test_event_endpoint_revises_a_live_run():
    orchestrator = Orchestrator()
    run = orchestrator.run(_request("example_1.json"))
    assert run.output is not None

    payload = json.loads((EXAMPLES / "event_track_shift.json").read_text(encoding="utf-8"))
    response = orchestrator.apply_event(run.run_id, Event(**payload["event"]))

    assert response.changed_beliefs
    assert response.invalidated_decisions
    assert response.preserved_decisions
    assert response.revised_plan is not None
    assert response.previous_plan is not None
    assert response.diff is not None
    assert response.diff.added or response.diff.removed or response.diff.changed


def test_unknown_run_id_is_a_clean_error():
    with pytest.raises(KeyError):
        Orchestrator().apply_event("no-such-run", Event(type="road_closure", payload={}))


def test_failure_returns_a_structured_error_not_a_stack_trace():
    """No Zetaris, no cache: the run must fail cleanly and say it is recoverable."""
    settings = load_settings()
    settings.sample_mode = True
    orchestrator = Orchestrator(settings)
    request = _request("example_1.json", sample_mode=True)
    request.input.scenario = "nonexistent_scenario"

    import app.adapters.zetaris as zmod

    original = zmod.ZetarisClient._from_cache

    def missing(self, sql, query_name, predicates=None):
        raise zmod.ZetarisUnavailable("no cached result for this query")

    zmod.ZetarisClient._from_cache = missing
    try:
        response = orchestrator.run(request)
    finally:
        zmod.ZetarisClient._from_cache = original

    assert response.status == "failed"
    assert response.error is not None
    assert response.error.type == "zetaris_unavailable"
    assert response.error.recoverable is True
    assert response.output is None
