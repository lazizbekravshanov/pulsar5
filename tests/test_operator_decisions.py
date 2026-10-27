"""An operator approves or declines the actions the Risk agent held for a
human. The decision is recorded against the run, written to the trace and to
H-MEM, and dropped again if a change of mind invalidates the action."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.engines.hmem.engine import HMem

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "input_examples"

SHELTER_FULL = {
    "type": "shelter_capacity",
    "payload": {"shelter_id": "S-071-2", "capacity": 1500, "current_occupancy": 1500,
                "status": "at_capacity", "updated_at": "2022-09-27T14:00Z"},
}

TRACK_SOUTH = {
    "type": "advisory_update",
    "payload": {
        "advisory_number": 23, "issued_at": "2022-09-27T21:00Z",
        "center_lat": 25.4, "center_lon": -82.9, "max_wind_kt": 130,
        "forecast_landfall_county": "Lee",
        "cone_polygon": [[27.45, -82.75], [27.45, -81.55], [26.60, -81.40],
                         [26.10, -81.70], [26.15, -82.40], [26.90, -82.80]],
    },
}


@pytest.fixture
def client(tmp_path, monkeypatch):
    store = tmp_path / "store.json"
    monkeypatch.setattr("app.orchestrator.HMem", lambda *a, **k: HMem(store))
    from app.main import app
    with TestClient(app) as c:
        c.store = store  # type: ignore[attr-defined]
        yield c


def _decide(client, body, **fields):
    return client.post("/runs/op-test/decisions", json={"plan_id": body["output"]["plan"]["plan_id"], **fields})


def _plan(client, run_id="op-test"):
    payload = json.loads((EXAMPLES / "example_1.json").read_text(encoding="utf-8"))
    payload["run_id"] = run_id
    res = client.post("/run", json=payload)
    assert res.status_code == 200, res.text
    body = res.json()
    held = [e["action_id"] for e in body["output"]["escalations"]]
    committed = [a["action_id"] for a in body["output"]["plan"]["actions"] if a["action_id"] not in held]
    assert held and committed
    return body, held, committed


def test_approving_a_held_action_records_it_and_shrinks_the_queue(client):
    body, held, _ = _plan(client)

    res = _decide(client, body, action_id=held[0], decision="approve")

    assert res.status_code == 200, res.text
    out = res.json()
    assert out["decision"]["action_id"] == held[0]
    assert out["decision"]["decision"] == "approve"
    assert out["decision"]["decided_at"]
    assert out["pending"] == len(held) - 1

    listed = client.get("/runs/op-test/decisions").json()
    assert [d["action_id"] for d in listed["decisions"]] == [held[0]]
    assert listed["pending"] == len(held) - 1


def test_approval_marks_the_world_model_decision_committed(client):
    body, held, _ = _plan(client)

    _decide(client, body, action_id=held[0], decision="approve")

    world = client.get("/runs/op-test/world").json()
    assert world["decisions"][held[0]]["status"] == "committed"


def test_declining_writes_a_trace_line_and_a_memory_episode(client):
    body, held, _ = _plan(client)
    note = "Shelter staff cannot reach it before the bridge closes"

    res = _decide(client, body, action_id=held[1], decision="decline", note=note)
    assert res.status_code == 200, res.text

    trace = client.get("/runs/op-test/trace").text
    lines = [json.loads(line) for line in trace.splitlines() if line.strip()]
    mine = [line for line in lines if line["agent_name"] == "Operator" and line["action"] == "decline_action"]
    assert len(mine) == 1
    assert held[1] in mine[0]["output_summary"]
    assert note in mine[0]["input_summary"]

    episodes = [m for m in HMem(client.store).all_items() if "operator" in m.tags]
    assert len(episodes) == 1
    assert note in episodes[0].statement
    assert episodes[0].run_id == "op-test"


def test_deciding_an_action_that_was_not_held_is_refused(client):
    body, _, committed = _plan(client)

    res = _decide(client, body, action_id=committed[0], decision="approve")

    assert res.status_code == 409
    assert committed[0] in res.json()["detail"]


def test_unknown_run_and_unknown_action_are_404(client):
    body, held, _ = _plan(client)
    pid = body["output"]["plan"]["plan_id"]

    assert client.post("/runs/nope/decisions", json={"plan_id": pid, "action_id": held[0], "decision": "approve"}).status_code == 404
    assert _decide(client, body, action_id="ACT-999", decision="approve").status_code == 404


def test_a_second_decision_on_the_same_action_replaces_the_first(client):
    body, held, _ = _plan(client)

    _decide(client, body, action_id=held[0], decision="approve")
    res = _decide(client, body, action_id=held[0], decision="decline")

    assert res.status_code == 200
    listed = client.get("/runs/op-test/decisions").json()
    assert [(d["action_id"], d["decision"]) for d in listed["decisions"]] == [(held[0], "decline")]


def test_change_of_mind_carries_decisions_by_action_not_by_number(client):
    """A revision renumbers actions, so an approval must follow the action it
    was given for (kind + target) when that action was preserved, and never
    attach itself to whatever now carries the old number."""
    body, held, _ = _plan(client)
    pid = _plan_id(body)
    old = {a["action_id"]: (a["kind"], a["target"]) for a in body["output"]["plan"]["actions"]}
    for action_id in held:
        assert client.post("/runs/op-test/decisions",
                           json={"plan_id": pid, "action_id": action_id, "decision": "approve"}).status_code == 200
    approved = {old[i] for i in held}

    res = client.post("/events", json={"run_id": "op-test", "event": SHELTER_FULL})
    assert res.status_code == 200, res.text
    event = res.json()
    invalidated = {old[i] for i in event["invalidated_decisions"]}
    revised = {a["action_id"]: (a["kind"], a["target"]) for a in event["revised_plan"]["actions"]}
    still_held = {e["action_id"] for e in event["escalations"]}

    listed = client.get("/runs/op-test/decisions").json()
    kept = {d["action_id"]: (d["kind"], d["target"]) for d in listed["decisions"]}

    assert kept, "approvals on actions the revision preserved must survive"
    for new_id, pair in kept.items():
        assert revised[new_id] == pair, f"{new_id} now carries {revised[new_id]}, decision says {pair}"
        assert pair in approved
        assert pair not in invalidated
        assert new_id in still_held
    assert listed["pending"] == len(still_held) - len(kept)


def _plan_id(body):
    return body["output"]["plan"]["plan_id"]


def test_a_decision_names_the_plan_it_answers(client):
    body, held, _ = _plan(client)

    missing = client.post("/runs/op-test/decisions", json={"action_id": held[0], "decision": "approve"})
    assert missing.status_code == 422

    stale = client.post("/runs/op-test/decisions",
                        json={"action_id": held[0], "decision": "approve", "plan_id": "plan-from-yesterday"})
    assert stale.status_code == 409
    assert "plan" in stale.json()["detail"]

    right = client.post("/runs/op-test/decisions",
                        json={"action_id": held[0], "decision": "approve", "plan_id": _plan_id(body)})
    assert right.status_code == 200


def test_changing_a_decision_leaves_the_world_model_consistent(client):
    body, held, _ = _plan(client)
    pid = _plan_id(body)
    post = lambda **kw: client.post("/runs/op-test/decisions", json={"plan_id": pid, **kw})  # noqa: E731

    post(action_id=held[0], decision="decline", note="first reason")
    post(action_id=held[0], decision="approve")
    world = client.get("/runs/op-test/world").json()["decisions"][held[0]]
    assert world["status"] == "committed"
    assert world["invalidated_reason"] is None

    post(action_id=held[1], decision="decline", note="first reason", operator="op1")
    post(action_id=held[1], decision="decline", note="second reason", operator="op2")
    world = client.get("/runs/op-test/world").json()["decisions"][held[1]]
    assert world["status"] == "invalidated"
    assert "second reason" in world["invalidated_reason"]
    assert "op2" in world["invalidated_reason"]


def test_memory_keeps_only_the_final_verdict_on_an_action(client):
    body, held, _ = _plan(client)
    pid = _plan_id(body)

    client.post("/runs/op-test/decisions", json={"plan_id": pid, "action_id": held[0], "decision": "decline", "note": "no staff"})
    client.post("/runs/op-test/decisions", json={"plan_id": pid, "action_id": held[0], "decision": "approve"})

    episodes = [m for m in HMem(client.store).all_items() if "operator" in m.tags]
    assert len(episodes) == 1
    assert "approved" in episodes[0].statement


def test_notes_and_names_are_capped_server_side(client):
    body, held, _ = _plan(client)
    pid = _plan_id(body)

    long_note = client.post("/runs/op-test/decisions",
                            json={"plan_id": pid, "action_id": held[0], "decision": "approve", "note": "x" * 201})
    long_name = client.post("/runs/op-test/decisions",
                            json={"plan_id": pid, "action_id": held[0], "decision": "approve", "operator": "y" * 41})

    assert long_note.status_code == 422
    assert long_name.status_code == 422


def test_a_decision_on_an_invalidated_action_does_not_follow_it_into_the_new_plan(client):
    """Advisory 23 moves the cone. Evacuations that cited the old cone are
    invalidated; an approval given against the old cone must not reappear on
    the same zone in the revised plan, even though it is still held there."""
    body, held, _ = _plan(client)
    pid = _plan_id(body)
    old = {a["action_id"]: (a["kind"], a["target"]) for a in body["output"]["plan"]["actions"]}
    for action_id in held:
        assert client.post("/runs/op-test/decisions",
                           json={"plan_id": pid, "action_id": action_id, "decision": "approve"}).status_code == 200

    event = client.post("/events", json={"run_id": "op-test", "event": TRACK_SOUTH}).json()
    invalidated_held = {old[i] for i in event["invalidated_decisions"] if i in held}
    revised = {(a["kind"], a["target"]): a["action_id"] for a in event["revised_plan"]["actions"]}
    still_held = {e["action_id"] for e in event["escalations"]}
    assert invalidated_held, "the shift must invalidate at least one held evacuation"

    kept = {(d["kind"], d["target"]) for d in client.get("/runs/op-test/decisions").json()["decisions"]}
    assert not (kept & invalidated_held)
    # and the ones that were preserved did carry, under their new numbers
    preserved_held = {old[i] for i in held} - invalidated_held
    assert kept == {p for p in preserved_held if revised.get(p) in still_held}
