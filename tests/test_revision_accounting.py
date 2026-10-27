"""The change-of-mind numbers must be about the plan on the table.

Risk stamps every final-plan decision `escalated` or `committed`, and earlier
Planner iterations leave `active` records behind. The Belief Reviser has to
look at the live plan, not at those leftovers, and a revision must never reuse
an action number that the World Model already knows."""

import json
from pathlib import Path

from app.engines.world_model.engine import Provenance, WorldModel
from app.orchestrator import Orchestrator
from app.schemas import Event, RunRequest

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "input_examples"
LIVE = {"active", "escalated", "committed"}


def _request(run_id: str) -> RunRequest:
    payload = json.loads((EXAMPLES / "example_1.json").read_text(encoding="utf-8"))
    payload["run_id"] = run_id
    return RunRequest(**payload)


def _track_south() -> Event:
    return Event(**json.loads((EXAMPLES / "event_track_shift.json").read_text(encoding="utf-8"))["event"])


def _prov() -> Provenance:
    return Provenance(source_table="t", query=None, as_of="2022-09-26T12:00Z",
                      retrieved_at="2022-09-26T12:00Z", data_source="test")


def test_dependents_include_committed_and_escalated_decisions():
    world = WorldModel()
    world.assert_belief("shelter:S-1:occupancy", "shelter:S-1", "current_occupancy", 100, _prov())
    for decision_id, status in (("d-committed", "committed"), ("d-escalated", "escalated"),
                                ("d-active", "active"), ("d-superseded", "superseded"), ("d-gone", "invalidated")):
        world.record_decision(decision_id, "open_shelter", "S-1", "x", depends_on=["shelter:S-1:occupancy"])
        world.set_decision_status(decision_id, status)

    dependents = {d.decision_id for d in world.depends_on_any(["shelter:S-1:occupancy"])}

    assert dependents == {"d-committed", "d-escalated", "d-active"}


def test_a_finished_run_leaves_only_the_final_plan_live(tmp_path, monkeypatch):
    monkeypatch.setattr("app.orchestrator.HMem", lambda *a, **k: __import__("app.engines.hmem.engine", fromlist=["HMem"]).HMem(tmp_path / "s.json"))
    orchestrator = Orchestrator()
    response = orchestrator.run(_request("acct-live"))
    assert response.output is not None
    assert response.iterations > 1, "this example needs a Critic rejection to leave a superseded iteration behind"

    from app.orchestrator import RUNS
    world = RUNS["acct-live"].world
    live = {d.decision_id for d in world.decisions() if d.status in LIVE}

    assert live == {a.action_id for a in response.output.plan.actions}
    assert not world.decisions(status="active"), "superseded iterations must not stay active"


def test_change_of_mind_accounts_for_the_plan_on_the_table(tmp_path, monkeypatch):
    monkeypatch.setattr("app.orchestrator.HMem", lambda *a, **k: __import__("app.engines.hmem.engine", fromlist=["HMem"]).HMem(tmp_path / "s.json"))
    orchestrator = Orchestrator()
    response = orchestrator.run(_request("acct-event"))
    plan_ids = {a.action_id for a in response.output.plan.actions}

    event = orchestrator.apply_event("acct-event", _track_south())
    invalidated, preserved = set(event.invalidated_decisions), set(event.preserved_decisions)

    assert invalidated, "a track shift must invalidate something"
    assert invalidated <= plan_ids
    assert preserved <= plan_ids
    assert not (invalidated & preserved)
    assert invalidated | preserved == plan_ids


def test_a_revision_never_reuses_an_action_number(tmp_path, monkeypatch):
    monkeypatch.setattr("app.orchestrator.HMem", lambda *a, **k: __import__("app.engines.hmem.engine", fromlist=["HMem"]).HMem(tmp_path / "s.json"))
    orchestrator = Orchestrator()
    response = orchestrator.run(_request("acct-ids"))
    old_ids = {a.action_id for a in response.output.plan.actions}

    event = orchestrator.apply_event("acct-ids", _track_south())
    new_ids = {a.action_id for a in event.revised_plan.actions}

    from app.orchestrator import RUNS
    world = RUNS["acct-ids"].world
    assert not (old_ids & new_ids)
    for decision_id in event.invalidated_decisions:
        assert world.snapshot()["decisions"][decision_id]["status"] == "invalidated"
    live = {d.decision_id for d in world.decisions() if d.status in LIVE}
    assert live == new_ids


def test_a_track_shift_spares_evacuations_whose_zone_stays_in_the_cone(tmp_path, monkeypatch):
    """An evacuation depends on its zone's cone membership, not on the polygon
    itself. When the track moves, the zones that left the cone are invalidated
    and the zones inside both cones keep their decisions."""
    from app.engines.hmem.engine import HMem
    from app.sim.geo import point_in_polygon

    monkeypatch.setattr("app.orchestrator.HMem", lambda *a, **k: HMem(tmp_path / "s.json"))
    orchestrator = Orchestrator()
    response = orchestrator.run(_request("acct-zones"))
    evacuations = {a.action_id: a.target for a in response.output.plan.actions if a.kind == "evacuate_zone"}
    assert evacuations

    event_obj = _track_south()
    event = orchestrator.apply_event("acct-zones", event_obj)
    from app.orchestrator import RUNS
    world = RUNS["acct-zones"].world
    new_cone = [(float(p[0]), float(p[1])) for p in event_obj.payload["cone_polygon"]]

    # The world now holds the new cone; the old membership is what the plan recorded.
    was_in = {i: "inside the forecast cone" in a.detail for i, a in
              ((a.action_id, a) for a in response.output.plan.actions if a.kind == "evacuate_zone")}
    now_in = {i: point_in_polygon((world.entity(z)["lat"], world.entity(z)["lon"]), new_cone) for i, z in evacuations.items()}
    stayed = {i for i in evacuations if was_in[i] and now_in[i]}
    left = {i for i in evacuations if was_in[i] and not now_in[i]}
    entered = {i for i in evacuations if not was_in[i] and now_in[i]}
    assert stayed and left, "the fixture needs zones on both sides of the shift"

    invalidated = set(event.invalidated_decisions)
    assert left <= invalidated, "a zone that left the cone must be revisited"
    assert entered <= invalidated, "a zone ordered on the buffer rule that is now inside must be revisited"
    assert not (stayed & invalidated), "a zone inside both cones keeps its decision"


def test_a_refreshed_timestamp_with_the_same_value_is_not_a_change(tmp_path, monkeypatch):
    """Re-asserting a belief with a newer as_of but the same value must not
    invalidate the decisions that cite it."""
    from app.engines.hmem.engine import HMem

    monkeypatch.setattr("app.orchestrator.HMem", lambda *a, **k: HMem(tmp_path / "s.json"))
    orchestrator = Orchestrator()
    response = orchestrator.run(_request("acct-asof"))
    from app.orchestrator import RUNS
    world = RUNS["acct-asof"].world
    same_cone = world.belief_value("hazard:cone_polygon")
    advisory = world.belief_value("hazard:advisory_number")

    event = orchestrator.apply_event("acct-asof", Event(type="advisory_update", payload={
        "advisory_number": advisory, "issued_at": "2022-09-26T18:00Z", "cone_polygon": same_cone,
    }))

    assert event.invalidated_decisions == []
    assert len(event.preserved_decisions) == len(response.output.plan.actions)
