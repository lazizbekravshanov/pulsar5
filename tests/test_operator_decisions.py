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
    _body, held, _ = _plan(client)

    res = client.post("/runs/op-test/decisions", json={"action_id": held[0], "decision": "approve"})

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
    _, held, _ = _plan(client)

    client.post("/runs/op-test/decisions", json={"action_id": held[0], "decision": "approve"})

    world = client.get("/runs/op-test/world").json()
    assert world["decisions"][held[0]]["status"] == "committed"


def test_declining_writes_a_trace_line_and_a_memory_episode(client):
    _body, held, _ = _plan(client)
    note = "Shelter staff cannot reach it before the bridge closes"

    res = client.post("/runs/op-test/decisions",
                      json={"action_id": held[1], "decision": "decline", "note": note})
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
    _, _, committed = _plan(client)

    res = client.post("/runs/op-test/decisions", json={"action_id": committed[0], "decision": "approve"})

    assert res.status_code == 409
    assert committed[0] in res.json()["detail"]


def test_unknown_run_and_unknown_action_are_404(client):
    _, held, _ = _plan(client)

    assert client.post("/runs/nope/decisions", json={"action_id": held[0], "decision": "approve"}).status_code == 404
    assert client.post("/runs/op-test/decisions", json={"action_id": "ACT-999", "decision": "approve"}).status_code == 404


def test_a_second_decision_on_the_same_action_replaces_the_first(client):
    _, held, _ = _plan(client)

    client.post("/runs/op-test/decisions", json={"action_id": held[0], "decision": "approve"})
    res = client.post("/runs/op-test/decisions", json={"action_id": held[0], "decision": "decline"})

    assert res.status_code == 200
    listed = client.get("/runs/op-test/decisions").json()
    assert [(d["action_id"], d["decision"]) for d in listed["decisions"]] == [(held[0], "decline")]


def test_change_of_mind_carries_decisions_by_action_not_by_number(client):
    """A revision renumbers actions, so an approval must follow the action it
    was given for (kind + target), drop when that action is invalidated or gone,
    and never attach itself to whatever now carries the old number."""
    body, held, _ = _plan(client)
    old = {a["action_id"]: (a["kind"], a["target"]) for a in body["output"]["plan"]["actions"]}
    for action_id in held:
        assert client.post("/runs/op-test/decisions",
                           json={"action_id": action_id, "decision": "approve"}).status_code == 200
    approved = {old[i] for i in held}

    res = client.post("/events", json={"run_id": "op-test", "event": TRACK_SOUTH})
    assert res.status_code == 200, res.text
    event = res.json()
    invalidated = {old[i] for i in event["invalidated_decisions"] if i in old}
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
    assert set(kept.values()) < approved, "something the operator approved must have dropped out"
    assert listed["pending"] == len(still_held) - len(kept)
