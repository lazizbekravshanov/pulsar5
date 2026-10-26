"""FastAPI surface. Judges must be able to curl this, so the HTTP contract is
the primary entry point and the UI is a thin reader on top of it.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from app.adapters.nightshift import NightshiftClient
from app.adapters.zetaris import ZetarisClient
from app.config import REPO_ROOT, load_settings
from app.orchestrator import RUNS, Orchestrator
from app.schemas import EventRequest, EventResponse, RunRequest, RunResponse

app = FastAPI(
    title="Pulsar 5",
    version="0.1.0",
    description=(
        "Disaster response planning agent. Zetaris is the data layer, Meterless "
        "Nightshift is the model provider, and every decision is traceable to evidence."
    ),
)
_orchestrator = Orchestrator()


@app.get("/health")
def health() -> dict:
    settings = load_settings()
    zetaris = ZetarisClient(settings)
    nightshift = NightshiftClient(settings)
    try:
        return {
            "status": "ok",
            "sample_mode": settings.sample_mode,
            "max_replan_iterations": settings.max_replan_iterations,
            "zetaris": zetaris.health(),
            "nightshift": nightshift.health(),
            "lanes": {
                "fast": settings.model_for_lane("fast"),
                "deep": settings.model_for_lane("deep"),
                "judge": settings.model_for_lane("judge"),
            },
            "active_runs": sorted(RUNS),
        }
    finally:
        zetaris.close()
        nightshift.close()


@app.post("/run", response_model=RunResponse)
def run(request: RunRequest) -> RunResponse:
    response = _orchestrator.run(request)
    if response.status == "failed":
        return JSONResponse(status_code=502, content=response.model_dump(mode="json"))  # type: ignore[return-value]
    return response


@app.post("/events", response_model=EventResponse)
def events(request: EventRequest) -> EventResponse:
    try:
        return _orchestrator.apply_event(request.run_id, request.event)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/runs/{run_id}")
def get_run(run_id: str) -> dict:
    state = RUNS.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"unknown run_id {run_id!r}")
    return {
        "run_id": run_id,
        "replay_hour": state.replay_hour,
        "plan": state.plan.model_dump(mode="json"),
        "escalations": [e.model_dump(mode="json") for e in state.escalations],
        "memory_recalled": state.lessons,
        "world": state.world.digest(),
        "trace_id": state.tracer.trace_id,
        "log_file": str(state.tracer.log_file),
    }


@app.get("/runs/{run_id}/trace", response_class=PlainTextResponse)
def get_trace(run_id: str) -> str:
    state = RUNS.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"unknown run_id {run_id!r}")
    path = Path(state.tracer.log_file)
    if not path.exists():
        raise HTTPException(status_code=404, detail="trace file not written yet")
    return path.read_text(encoding="utf-8")


@app.get("/runs/{run_id}/world")
def get_world(run_id: str) -> dict:
    state = RUNS.get(run_id)
    if state is None:
        raise HTTPException(status_code=404, detail=f"unknown run_id {run_id!r}")
    return state.world.snapshot()


@app.get("/")
def ui() -> FileResponse:
    index = REPO_ROOT / "ui" / "index.html"
    if not index.exists():
        raise HTTPException(status_code=404, detail="UI not built")
    return FileResponse(index)
