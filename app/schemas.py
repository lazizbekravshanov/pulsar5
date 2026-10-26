"""Pydantic contracts for the API, the plan and the event stream."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

ActionKind = Literal["open_shelter", "stage_supplies", "evacuate_zone", "hold"]
EventType = Literal["advisory_update", "shelter_capacity", "road_closure"]


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------- input


class RunConstraints(BaseModel):
    supply_trucks: int = 12
    staging_sites_max: int = 4


class RunInput(BaseModel):
    scenario: str = "ian_2022"
    region: list[str] = Field(default_factory=lambda: ["Lee", "Charlotte", "Sarasota", "Pinellas", "Hillsborough"])
    replay_hour: str = "2022-09-26T12:00Z"
    goal: str = "Plan shelter openings, supply staging and evacuation priority for the next 24 hours."
    constraints: RunConstraints = Field(default_factory=RunConstraints)


class RunOptions(BaseModel):
    sample_mode: bool | None = None
    max_iterations: int | None = None
    replay_steps: int | None = None


class RunRequest(BaseModel):
    run_id: str
    input: RunInput = Field(default_factory=RunInput)
    options: RunOptions = Field(default_factory=RunOptions)


# ---------------------------------------------------------------- plan


class PlanAction(BaseModel):
    action_id: str
    kind: ActionKind
    target: str
    detail: str
    priority: int = 5
    reversible: bool = True
    confidence: float = 0.5
    evidence_ids: list[str] = Field(default_factory=list)
    rationale: str = ""


class Plan(BaseModel):
    plan_id: str
    replay_hour: str
    actions: list[PlanAction] = Field(default_factory=list)
    summary: str = ""
    produced_by: str = "Planner"

    def by_id(self) -> dict[str, PlanAction]:
        return {a.action_id: a for a in self.actions}


class Violation(BaseModel):
    code: str
    severity: Literal["low", "medium", "high"]
    message: str
    action_id: str | None = None
    zone_id: str | None = None
    observed: float | None = None
    limit: float | None = None


class SimulationResult(BaseModel):
    ok: bool
    violations: list[Violation] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)


class CritiqueFinding(BaseModel):
    code: str
    message: str
    action_id: str | None = None


class Critique(BaseModel):
    accepted: bool
    findings: list[CritiqueFinding] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    confidence: float = 0.5
    reviewed_by_model: str | None = None


class Escalation(BaseModel):
    action_id: str
    reason: str
    confidence: float
    reversible: bool
    requires: Literal["human_approval"] = "human_approval"


# ---------------------------------------------------------------- output


class AgentCard(BaseModel):
    name: str
    role: str
    lane: str


class RunOutput(BaseModel):
    plan: Plan
    escalations: list[Escalation] = Field(default_factory=list)
    unresolved_issues: list[str] = Field(default_factory=list)
    simulation: SimulationResult | None = None
    critique: Critique | None = None
    memory_recalled: list[str] = Field(default_factory=list)


class RunResponse(BaseModel):
    run_id: str
    status: Literal["completed", "escalated", "failed"]
    output: RunOutput | None = None
    agents: list[AgentCard] = Field(default_factory=list)
    iterations: int = 0
    models_used: list[str] = Field(default_factory=list)
    models_attempted: list[str] = Field(default_factory=list)
    engines_used: list[str] = Field(default_factory=list)
    zetaris_queries: int = 0
    trace_id: str = ""
    log_file: str = ""
    execution_time_seconds: float = 0.0
    sample_mode: bool = False
    error: "StructuredError | None" = None


class StructuredError(BaseModel):
    type: str
    message: str
    recoverable: bool


# ---------------------------------------------------------------- events


class Event(BaseModel):
    type: EventType
    payload: dict[str, Any] = Field(default_factory=dict)


class EventRequest(BaseModel):
    run_id: str
    event: Event


class BeliefChange(BaseModel):
    fact_id: str
    field: str
    before: Any = None
    after: Any = None


class PlanDiff(BaseModel):
    added: list[PlanAction] = Field(default_factory=list)
    removed: list[str] = Field(default_factory=list)
    changed: list[dict[str, Any]] = Field(default_factory=list)


class EventResponse(BaseModel):
    run_id: str
    event_type: EventType
    changed_beliefs: list[BeliefChange] = Field(default_factory=list)
    invalidated_decisions: list[str] = Field(default_factory=list)
    preserved_decisions: list[str] = Field(default_factory=list)
    revised_plan: Plan | None = None
    previous_plan: Plan | None = None
    diff: PlanDiff | None = None
    escalations: list[Escalation] = Field(default_factory=list)
    trace_id: str = ""
    execution_time_seconds: float = 0.0


RunResponse.model_rebuild()
