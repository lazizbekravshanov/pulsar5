"""Runs outlive the process that made them: a run is written to a store on
every change and read back by whichever instance gets the next request."""

import json
from pathlib import Path

import pytest

from app.engines.hmem.engine import HMem
from app.orchestrator import RUNS, Orchestrator, RunState
from app.schemas import OperatorDecisionRequest, RunRequest

REPO = Path(__file__).resolve().parent.parent
EXAMPLES = REPO / "input_examples"


def _request(run_id: str, name: str = "example_1.json") -> RunRequest:
    payload = json.loads((EXAMPLES / name).read_text(encoding="utf-8"))
    payload["run_id"] = run_id
    return RunRequest(**payload)


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """Traces and memory under tmp, and no run left behind in the cache."""
    monkeypatch.setattr("app.tracing.LOGS_DIR", tmp_path / "logs")
    monkeypatch.setattr("app.orchestrator.HMem", lambda *a, **k: HMem(tmp_path / "hmem.json"))
    RUNS.clear()
    yield
    RUNS.clear()


def _decided_run(orchestrator: Orchestrator, run_id: str) -> RunState:
    response = orchestrator.run(_request(run_id))
    assert response.output is not None
    held = response.output.escalations[0].action_id
    orchestrator.decide(
        run_id,
        OperatorDecisionRequest(plan_id=response.output.plan.plan_id, action_id=held, decision="approve", note="go"),
    )
    return RUNS[run_id]


def _without_saved_at(record: dict) -> dict:
    return {k: v for k, v in record.items() if k != "saved_at"}


# ----------------------------------------------------------------- record


def test_run_state_record_round_trips(tmp_path):
    state = _decided_run(Orchestrator(), "rec-test")

    record = state.to_record()
    json.dumps(record)  # the store keeps JSON, so the record must be plain data
    rebuilt = RunState.from_record(record)

    assert rebuilt.run_id == "rec-test"
    assert rebuilt.plan == state.plan
    assert rebuilt.escalations == state.escalations
    assert rebuilt.decisions == state.decisions
    assert rebuilt.replay_hour == state.replay_hour
    assert rebuilt.region == state.region
    assert rebuilt.goal == state.goal
    assert rebuilt.scenario == state.scenario
    assert rebuilt.lessons == state.lessons
    assert rebuilt.constraints == state.constraints
    assert rebuilt.world.snapshot() == state.world.snapshot()
    assert rebuilt.tracer.trace_id == state.tracer.trace_id
    assert Path(rebuilt.tracer.log_file) == Path(state.tracer.log_file)
    assert rebuilt.settings.sample_mode == state.settings.sample_mode
    assert rebuilt.settings.max_replan_iterations == state.settings.max_replan_iterations
    assert _without_saved_at(rebuilt.to_record()) == _without_saved_at(record)


def test_rebuilt_run_keeps_its_trace_and_keeps_appending(tmp_path):
    state = _decided_run(Orchestrator(), "trace-test")
    record = state.to_record()
    before = len(state.tracer.events)
    Path(state.tracer.log_file).unlink()  # the other instance never had the file

    rebuilt = RunState.from_record(record)

    text = Path(rebuilt.tracer.log_file).read_text(encoding="utf-8")
    assert text == record["trace"]["text"]
    assert len(rebuilt.tracer.events) == before
    assert any(e["action"] == "approve_action" for e in rebuilt.tracer.events)

    rebuilt.tracer.log(agent_name="Operator", action="decline_action")
    lines = Path(rebuilt.tracer.log_file).read_text(encoding="utf-8").splitlines()
    assert len(lines) == before + 1
    assert json.loads(lines[-1])["trace_id"] == state.tracer.trace_id


# ------------------------------------------------------------ file backend


def test_file_store_saves_and_loads_a_record(tmp_path):
    from app.run_store import FileRunStore

    store = FileRunStore(tmp_path / "runs")
    record = {"run_id": "file-test", "plan": {"plan_id": "p1"}, "nested": {"a": [1, 2]}}

    store.save("file-test", record)
    assert store.load("file-test") == record
    assert store.load("never-ran") is None
    assert store.name == "file"
    assert store.describe() == {"backend": "file", "location": str(tmp_path / "runs")}


def test_file_store_keeps_every_run_inside_its_directory(tmp_path):
    from app.run_store import FileRunStore

    store = FileRunStore(tmp_path / "runs")
    store.save("../escaped/../run", {"run_id": "x"})

    written = list((tmp_path / "runs").glob("*.json"))
    assert len(written) == 1
    assert not (tmp_path / "escaped").exists()
    assert store.load("../escaped/../run") == {"run_id": "x"}


# ------------------------------------------------------------- selection


def test_store_is_a_file_store_until_a_blob_token_is_set(tmp_path, monkeypatch):
    from app.run_store import FileRunStore, VercelBlobRunStore, open_run_store

    monkeypatch.delenv("BLOB_READ_WRITE_TOKEN", raising=False)
    monkeypatch.setattr("app.run_store.LOGS_DIR", tmp_path / "logs")
    store = open_run_store()
    assert isinstance(store, FileRunStore)
    assert store.describe()["location"] == str(tmp_path / "logs" / "runs")

    monkeypatch.setenv("BLOB_READ_WRITE_TOKEN", "vercel_blob_rw_teststore_notasecret")
    store = open_run_store()
    assert isinstance(store, VercelBlobRunStore)
    assert store.name == "vercel_blob"


# ---------------------------------------------------------- blob backend

FAKE_TOKEN = "vercel_blob_rw_teststore_notasecret"


def _blob_store(handler):
    import httpx

    from app.run_store import VercelBlobRunStore

    return VercelBlobRunStore(FAKE_TOKEN, client=httpx.Client(transport=httpx.MockTransport(handler)))


def test_blob_store_writes_a_private_blob_the_way_the_sdk_does():
    import httpx

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["url"] = str(request.url)
        seen["headers"] = dict(request.headers)
        seen["body"] = request.content
        return httpx.Response(200, json={"url": "https://teststore.private.blob.vercel-storage.com/x", "pathname": "x"})

    record = {"run_id": "blob-test", "plan": {"plan_id": "p1"}}
    _blob_store(handler).save("blob-test", record)

    assert seen["method"] == "PUT"
    assert seen["url"] == "https://vercel.com/api/blob/?pathname=pulsar5%2Fruns%2Fblob-test.json"
    assert json.loads(seen["body"]) == record
    headers = seen["headers"]
    assert headers["authorization"] == f"Bearer {FAKE_TOKEN}"
    assert headers["x-vercel-blob-access"] == "private"
    assert headers["x-vercel-blob-store-id"] == "teststore"
    assert headers["x-api-version"] == "13"
    assert headers["x-content-type"] == "application/json"
    assert headers["x-add-random-suffix"] == "0"
    assert headers["x-allow-overwrite"] == "1"


def test_blob_store_reads_a_private_blob_uncached_and_misses_cleanly():
    import httpx

    record = {"run_id": "blob-test", "plan": {"plan_id": "p1"}}
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), request.headers.get("authorization")))
        if request.url.path.endswith("/blob-test.json"):
            return httpx.Response(200, json=record)
        return httpx.Response(404, text="not found")

    store = _blob_store(handler)
    assert store.load("blob-test") == record
    assert store.load("never-ran") is None

    assert seen[0] == (
        "GET",
        "https://teststore.private.blob.vercel-storage.com/pulsar5/runs/blob-test.json?cache=0",
        f"Bearer {FAKE_TOKEN}",
    )


def test_blob_store_failures_are_errors_that_never_carry_the_token():
    import httpx

    from app.run_store import RunStoreError, VercelBlobRunStore

    def refuse(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"error": {"code": "forbidden", "message": "bad token"}})

    store = _blob_store(refuse)
    with pytest.raises(RunStoreError) as saved:
        store.save("blob-test", {"run_id": "blob-test"})
    with pytest.raises(RunStoreError) as loaded:
        store.load("blob-test")
    assert FAKE_TOKEN not in str(saved.value) and FAKE_TOKEN not in str(loaded.value)
    assert "403" in str(saved.value)

    with pytest.raises(ValueError) as bad:
        VercelBlobRunStore("not-a-blob-token")
    assert "not-a-blob-token" not in str(bad.value)

    assert FAKE_TOKEN not in json.dumps(store.describe())
    assert store.describe()["backend"] == "vercel_blob"


# ---------------------------------------------------------- orchestrator

SHELTER_FULL = {
    "type": "shelter_capacity",
    "payload": {"shelter_id": "S-071-2", "capacity": 1500, "current_occupancy": 1500,
                "status": "at_capacity", "updated_at": "2022-09-27T14:00Z"},
}


def test_every_change_to_a_run_writes_through_to_the_store(tmp_path):
    from app.run_store import FileRunStore
    from app.schemas import Event

    store = FileRunStore(tmp_path / "runs")
    orchestrator = Orchestrator(store=store)
    response = orchestrator.run(_request("wt-test"))
    assert response.output is not None
    first = store.load("wt-test")
    assert first["plan"]["plan_id"] == response.output.plan.plan_id
    assert first["trace"]["text"].count("\n") == len(RUNS["wt-test"].tracer.events)

    event = orchestrator.apply_event("wt-test", Event(**SHELTER_FULL))
    after_event = store.load("wt-test")
    assert after_event["plan"]["plan_id"] == event.revised_plan.plan_id
    assert after_event["world"]["revision"] > first["world"]["revision"]

    held = event.escalations[0].action_id
    orchestrator.decide("wt-test", OperatorDecisionRequest(plan_id=event.revised_plan.plan_id, action_id=held,
                                                           decision="decline", note="no staff"))
    after_decision = store.load("wt-test")
    assert after_decision["decisions"][held]["decision"] == "decline"
    assert '"decline_action"' in after_decision["trace"]["text"]


def test_a_run_another_instance_made_is_served_from_the_store(tmp_path):
    """Each `RUNS.clear()` is a request landing on an instance that never saw
    the run. Event, decision and listing must all work from the store."""
    from app.run_store import FileRunStore
    from app.schemas import Event

    store = FileRunStore(tmp_path / "runs")
    response = Orchestrator(store=store).run(_request("miss-test"))
    assert response.output is not None
    trace_id = response.trace_id

    RUNS.clear()
    event = Orchestrator(store=store).apply_event("miss-test", Event(**SHELTER_FULL))
    assert event.trace_id == trace_id
    assert "miss-test" in RUNS, "a rehydrated run is cached for the next request on this instance"

    RUNS.clear()
    held = event.escalations[0].action_id
    decided = Orchestrator(store=store).decide(
        "miss-test",
        OperatorDecisionRequest(plan_id=event.revised_plan.plan_id, action_id=held, decision="approve"),
    )
    assert decided.decision is not None and decided.decision.action_id == held

    RUNS.clear()
    listed = Orchestrator(store=store).list_decisions("miss-test")
    assert [d.action_id for d in listed.decisions] == [held]
    assert RUNS["miss-test"].world.decisions(status="committed")

    with pytest.raises(KeyError):
        Orchestrator(store=store).apply_event("never-ran", Event(**SHELTER_FULL))


def test_a_store_outage_does_not_fail_the_run(tmp_path):
    from app.run_store import RunStoreError

    class Down:
        name = "down"

        def save(self, run_id, record):
            raise RunStoreError("store unreachable")

        def load(self, run_id):
            return None

        def describe(self):
            return {"backend": self.name, "location": "nowhere"}

    response = Orchestrator(store=Down()).run(_request("outage-test"))

    assert response.status in {"completed", "escalated"}
    failures = [e for e in RUNS["outage-test"].tracer.events if e["action"] == "run_store_write_failed"]
    assert len(failures) == 1 and failures[0]["status"] == "error"


# ------------------------------------------------------------------- api


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.run_store import FileRunStore

    store = FileRunStore(tmp_path / "runs")
    monkeypatch.setattr("app.main._orchestrator.store", store)
    with TestClient(app) as c:
        c.store = store  # type: ignore[attr-defined]
        yield c


def test_health_names_the_active_store_backend(client):
    health = client.get("/health").json()
    assert health["run_store"] == client.store.describe()
    assert "token" not in json.dumps(health).lower()


def test_the_api_serves_a_run_from_the_store_after_a_cache_miss(client):
    payload = json.loads((EXAMPLES / "example_1.json").read_text(encoding="utf-8"))
    payload["run_id"] = "api-miss"
    body = client.post("/run", json=payload).json()
    RUNS.clear()

    trace = client.get("/runs/api-miss/trace")
    assert trace.status_code == 200
    assert '"run_complete"' in trace.text
    assert client.get("/runs/api-miss").json()["trace_id"] == body["trace_id"]
    assert client.get("/runs/api-miss/world").json()["decisions"]

    RUNS.clear()
    event = client.post("/events", json={"run_id": "api-miss", "event": SHELTER_FULL})
    assert event.status_code == 200, event.text

    RUNS.clear()
    held = event.json()["escalations"][0]["action_id"]
    decided = client.post("/runs/api-miss/decisions", json={
        "plan_id": event.json()["revised_plan"]["plan_id"], "action_id": held, "decision": "approve"})
    assert decided.status_code == 200, decided.text

    assert client.get("/runs/never-ran").status_code == 404
    assert client.get("/runs/never-ran/trace").status_code == 404


def test_a_store_outage_on_a_read_is_a_503_not_a_crash(client, monkeypatch):
    from app.run_store import RunStoreError

    def down(run_id):
        raise RunStoreError("Vercel Blob could not be reached to load run")

    monkeypatch.setattr(client.store, "load", down)
    res = client.get("/runs/anything")
    assert res.status_code == 503
    assert "could not be reached" in res.json()["detail"]
