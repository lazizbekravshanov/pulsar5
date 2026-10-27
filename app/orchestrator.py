"""The agent loop.

One run is:

    H-MEM recall  ->  Markovian bounded step(s)  ->  for each step:
        Analyst (Zetaris)  ->  Planner  ->  [Simulator  ->  Critic]  ->  Risk
                                 ^______________revise______|

The Planner/Simulator/Critic cycle is bounded by `MAX_REPLAN_ITERATIONS`. If
the Critic still refuses at the cap, the run returns the best plan it has with
the unresolved findings stated and everything escalated, rather than looping or
pretending success.

Lessons go back into H-MEM at the end, so the next run starts better informed.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from app.adapters.nightshift import NightshiftClient
from app.adapters.zetaris import ZetarisClient, ZetarisUnavailable
from app.agents.analyst import Analyst
from app.agents.base import AgentContext
from app.agents.belief_reviser import BeliefReviser, diff_plans
from app.agents.critic import Critic
from app.agents.planner import Planner
from app.agents.risk import Risk
from app.agents.simulator import Simulator
from app.config import Settings, load_settings
from app.engines.hmem.engine import LEVEL_EPISODE, HMem
from app.engines.markovian.engine import MarkovianRunner
from app.engines.world_model.engine import WorldModel
from app.schemas import (
    Critique,
    DecisionResponse,
    Escalation,
    Event,
    EventResponse,
    OperatorDecision,
    OperatorDecisionRequest,
    Plan,
    RunOutput,
    RunRequest,
    RunResponse,
    SimulationResult,
    StructuredError,
)
from app.tracing import Tracer

MEMORY_DERATE = 0.75


@dataclass
class RunState:
    """What a finished run leaves behind so `/events` can revise it."""

    run_id: str
    settings: Settings
    world: WorldModel
    tracer: Tracer
    memory: HMem
    plan: Plan
    constraints: dict[str, Any]
    replay_hour: str
    region: list[str] = field(default_factory=list)
    goal: str = ""
    scenario: str = ""
    escalations: list[Escalation] = field(default_factory=list)
    lessons: list[str] = field(default_factory=list)
    # What a human has said about the held actions, keyed by action_id.
    decisions: dict[str, OperatorDecision] = field(default_factory=dict)


RUNS: dict[str, RunState] = {}


class Orchestrator:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()

    # --------------------------------------------------------------------- run

    def run(self, request: RunRequest) -> RunResponse:
        started = time.monotonic()
        settings = self._settings_for(request)
        tracer = Tracer(request.run_id, secrets=settings.secrets())
        world = WorldModel()
        memory = HMem()
        zetaris = ZetarisClient(settings, tracer)
        nightshift = NightshiftClient(settings, tracer)
        ctx = AgentContext(settings, tracer, world, zetaris, nightshift, memory)

        analyst, planner = Analyst(ctx), Planner(ctx)
        simulator, critic, risk = Simulator(ctx), Critic(ctx), Risk(ctx)
        agents = [analyst, planner, simulator, critic, risk, BeliefReviser(ctx)]

        tracer.log(
            agent_name="Orchestrator",
            action="run_start",
            input_summary=(
                f"scenario={request.input.scenario} hour={request.input.replay_hour} "
                f"region={','.join(request.input.region)}"
            ),
            output_summary=f"sample_mode={settings.sample_mode}, max_iterations={settings.max_replan_iterations}",
        )

        try:
            lessons = self._recall(memory, request, tracer, world)
            hours = self._replay_hours(request)
            runner = MarkovianRunner()
            final: dict[str, Any] = {}

            def step(index: int, label: str, context: dict[str, Any]) -> dict[str, Any]:
                nonlocal final
                tracer.engine_op(
                    "markovian",
                    "replay_step_start",
                    f"step {index}/{len(hours)} hour={label} context={len(str(context))} chars",
                )
                outcome = self._plan_one_hour(
                    replay_hour=label,
                    request=request,
                    settings=settings,
                    analyst=analyst,
                    planner=planner,
                    simulator=simulator,
                    critic=critic,
                    risk=risk,
                    lessons=lessons,
                )
                final = outcome
                plan: Plan = outcome["plan"]
                simulation: SimulationResult = outcome["simulation"]
                return {
                    "summary": plan.summary,
                    "digest": world.digest(),
                    "open_issues": [v.code for v in simulation.violations],
                    "iterations": outcome["iterations"],
                }

            runner.run(hours, step, initial_memory={"lessons": lessons} if lessons else None)
            tracer.engine_op("markovian", "replay_complete", str(runner.digest()))

            plan: Plan = final["plan"]
            simulation: SimulationResult = final["simulation"]
            critique: Critique = final["critique"]
            escalations: list[Escalation] = final["escalations"]
            # Acceptance means nothing *fixable* is left, not that nothing is
            # wrong. Whatever the Critic still holds against the plan travels
            # with it so the operator sees it.
            unresolved = [f"{f.code}: {f.message}" for f in critique.findings]

            self._write_lessons(memory, request, plan, simulation, critique, tracer)

            RUNS[request.run_id] = RunState(
                run_id=request.run_id,
                settings=settings,
                world=world,
                tracer=tracer,
                memory=memory,
                plan=plan,
                constraints=request.input.constraints.model_dump(),
                replay_hour=request.input.replay_hour,
                region=list(request.input.region),
                goal=request.input.goal,
                scenario=request.input.scenario,
                escalations=escalations,
                lessons=lessons,
            )

            status = "escalated" if escalations or unresolved else "completed"
            tracer.log(
                agent_name="Orchestrator",
                action="run_complete",
                output_summary=(
                    f"status={status} iterations={final['iterations']} "
                    f"actions={len(plan.actions)} escalations={len(escalations)}"
                ),
                confidence=critique.confidence,
            )

            return RunResponse(
                run_id=request.run_id,
                status=status,
                output=RunOutput(
                    plan=plan,
                    escalations=escalations,
                    unresolved_issues=unresolved,
                    simulation=simulation,
                    critique=critique,
                    memory_recalled=lessons,
                ),
                agents=[a.card for a in agents],
                iterations=final["iterations"],
                models_used=tracer.models_used,
                models_attempted=tracer.models_attempted,
                engines_used=sorted(set(tracer.engines_used) | {"world_model", "hmem", "markovian"}),
                zetaris_queries=tracer.zetaris_queries,
                trace_id=tracer.trace_id,
                log_file=str(tracer.log_file),
                execution_time_seconds=round(time.monotonic() - started, 3),
                sample_mode=settings.sample_mode,
            )

        except ZetarisUnavailable as exc:
            return self._failure(
                request, tracer, started, settings,
                StructuredError(type="zetaris_unavailable", message=str(exc), recoverable=True),
            )
        except Exception as exc:  # structured error, never a stack trace to the caller
            return self._failure(
                request, tracer, started, settings,
                StructuredError(type=type(exc).__name__, message=str(exc)[:500], recoverable=False),
            )
        finally:
            zetaris.close()
            nightshift.close()

    # ------------------------------------------------------------------- event

    def apply_event(self, run_id: str, event: Event) -> EventResponse:
        started = time.monotonic()
        state = RUNS.get(run_id)
        if state is None:
            raise KeyError(f"unknown run_id {run_id!r}; POST /run first")

        zetaris = ZetarisClient(state.settings, state.tracer)
        nightshift = NightshiftClient(state.settings, state.tracer)
        ctx = AgentContext(state.settings, state.tracer, state.world, zetaris, nightshift, state.memory)

        reviser = BeliefReviser(ctx)
        analyst = Analyst(ctx)
        planner, simulator, critic, risk = Planner(ctx), Simulator(ctx), Critic(ctx), Risk(ctx)

        try:
            state.tracer.log(
                agent_name="Orchestrator",
                action="event_received",
                input_summary=f"{event.type} on run {run_id}",
            )
            revision = reviser.apply_event(event)
            previous_plan = state.plan

            if not revision["invalidated_decisions"]:
                state.tracer.log(
                    agent_name="Orchestrator",
                    action="no_replan_needed",
                    output_summary=(
                        f"{len(revision['changed_beliefs'])} beliefs changed but no active decision "
                        "depended on them; plan stands"
                    ),
                )
                return EventResponse(
                    run_id=run_id,
                    event_type=event.type,
                    changed_beliefs=revision["changed_beliefs"],
                    invalidated_decisions=[],
                    preserved_decisions=revision["preserved_decisions"],
                    revised_plan=previous_plan,
                    previous_plan=previous_plan,
                    diff=diff_plans(previous_plan, previous_plan),
                    escalations=state.escalations,
                    trace_id=state.tracer.trace_id,
                    execution_time_seconds=round(time.monotonic() - started, 3),
                )

            # An advisory moves the clock. Facts gathered at the old hour are now
            # behind it, and refreshing them is the Analyst's job, not something
            # the Planner can revise its way out of. Re-gather first, then plan.
            if event.type == "advisory_update":
                new_hour = str(event.payload.get("issued_at") or state.replay_hour)
                if new_hour > state.replay_hour:
                    state.tracer.log(
                        agent_name="Orchestrator",
                        action="advance_replay_hour",
                        output_summary=f"{state.replay_hour} -> {new_hour}; re-gathering facts",
                        target_agent="Analyst",
                    )
                    state.replay_hour = new_hour
                    analyst.gather(
                        region=state.region, replay_hour=new_hour, goal=state.goal or "Replan after advisory update"
                    )

            revised, simulation, critique, iterations = self._refine(
                planner.propose(
                    replay_hour=state.replay_hour,
                    constraints=state.constraints,
                    lessons=state.lessons,
                ),
                planner=planner,
                simulator=simulator,
                critic=critic,
                replay_hour=state.replay_hour,
                constraints=state.constraints,
                lessons=state.lessons,
                max_iterations=state.settings.max_replan_iterations,
            )
            escalations = risk.classify(revised, critique)
            diff = diff_plans(previous_plan, revised)

            state.plan = revised
            state.escalations = escalations
            state.decisions = self._carry_decisions(state.decisions, revision["invalidated_decisions"], revised, escalations)
            for decision in state.decisions.values():
                self._apply_decision_to_world(state, decision)

            state.tracer.log(
                agent_name="Orchestrator",
                action="replan_complete",
                output_summary=(
                    f"{len(diff.added)} actions added, {len(diff.removed)} removed, "
                    f"{len(diff.changed)} changed; {revision['explanation']}"
                ),
                confidence=critique.confidence,
                extra={"iterations": iterations},
            )

            return EventResponse(
                run_id=run_id,
                event_type=event.type,
                changed_beliefs=revision["changed_beliefs"],
                invalidated_decisions=revision["invalidated_decisions"],
                preserved_decisions=revision["preserved_decisions"],
                revised_plan=revised,
                previous_plan=previous_plan,
                diff=diff,
                escalations=escalations,
                trace_id=state.tracer.trace_id,
                execution_time_seconds=round(time.monotonic() - started, 3),
            )
        finally:
            zetaris.close()
            nightshift.close()

    # ----------------------------------------------------------------- operator

    def decide(self, run_id: str, request: OperatorDecisionRequest) -> DecisionResponse:
        """Record a human's answer to an escalation.

        Only a held action can be decided: approving something that already
        committed on its own, or declining it, would be a different feature
        (a veto) and is refused rather than silently accepted.
        """
        state = RUNS.get(run_id)
        if state is None:
            raise KeyError(f"unknown run_id {run_id!r}; POST /run first")
        action = state.plan.by_id().get(request.action_id)
        if action is None:
            raise KeyError(f"unknown action_id {request.action_id!r} in run {run_id!r}")
        if request.action_id not in {e.action_id for e in state.escalations}:
            raise ValueError(
                f"{request.action_id} was not held for a human; only escalated actions can be decided"
            )

        decision = OperatorDecision(
            action_id=request.action_id,
            kind=action.kind,
            target=action.target,
            decision=request.decision,
            note=request.note.strip(),
            operator=request.operator.strip() or "operator",
        )
        state.decisions[request.action_id] = decision
        self._apply_decision_to_world(state, decision)

        verb = "approved" if decision.decision == "approve" else "declined"
        state.tracer.log(
            agent_name="Operator",
            action=f"{decision.decision}_action",
            input_summary=decision.note,
            output_summary=f"{action.action_id} {action.kind} {action.target} {verb} by {decision.operator}",
            confidence=action.confidence,
            extra={"action_id": action.action_id, "decision": decision.decision, "operator": decision.operator},
        )

        # The operator's call is an episode the next run can recall for the
        # same region, the same way a shelter filling early is.
        entity = state.world.entity(action.target) or {}
        county = str(entity.get("county") or "")
        tags = [state.scenario, *[c.lower() for c in state.region], county.lower(), "operator"]
        statement = f"Operator {verb} {action.kind} {action.target} at {state.replay_hour}"
        if decision.note:
            statement += f": {decision.note}"
        state.memory.remember(
            f"operator:{action.kind}:{action.target}",
            statement,
            level=LEVEL_EPISODE,
            tags=tags,
            confidence=0.8,
            run_id=run_id,
            evidence=action.evidence_ids,
        )
        state.memory.flush()
        state.tracer.engine_op("hmem", "write_memory", f"operator decision on {action.action_id} remembered")

        return self._decision_response(state, decision)

    def list_decisions(self, run_id: str) -> DecisionResponse:
        state = RUNS.get(run_id)
        if state is None:
            raise KeyError(f"unknown run_id {run_id!r}; POST /run first")
        return self._decision_response(state, None)

    @staticmethod
    def _carry_decisions(
        decisions: dict[str, OperatorDecision],
        invalidated: list[str],
        revised: Plan,
        escalations: list[Escalation],
    ) -> dict[str, OperatorDecision]:
        """Move operator decisions across a revision.

        The Planner numbers a revised plan afresh, so an approval is matched by
        what it was for (kind and target), not by action_id. It is dropped when
        the Belief Reviser invalidated the decision it answered, when the
        revised plan no longer contains that action, or when the action is no
        longer held for a human and there is nothing left to answer.
        """
        gone = set(invalidated)
        new_id = {(a.kind, a.target): a.action_id for a in revised.actions}
        held = {e.action_id for e in escalations}
        carried: dict[str, OperatorDecision] = {}
        for old_id, decision in decisions.items():
            if old_id in gone:
                continue
            target_id = new_id.get((decision.kind, decision.target))
            if target_id is None or target_id not in held:
                continue
            carried[target_id] = decision.model_copy(update={"action_id": target_id})
        return carried

    def _apply_decision_to_world(self, state: RunState, decision: OperatorDecision) -> None:
        if decision.decision == "approve":
            state.world.set_decision_status(decision.action_id, "committed")
        else:
            state.world.invalidate([decision.action_id], f"declined by {decision.operator}")

    def _decision_response(self, state: RunState, decision: OperatorDecision | None) -> DecisionResponse:
        pending = sum(1 for e in state.escalations if e.action_id not in state.decisions)
        return DecisionResponse(
            run_id=state.run_id,
            decision=decision,
            decisions=list(state.decisions.values()),
            pending=pending,
        )

    # ----------------------------------------------------------------- helpers

    def _settings_for(self, request: RunRequest) -> Settings:
        settings = load_settings()
        if request.options.sample_mode is not None:
            settings.sample_mode = request.options.sample_mode
        if request.options.max_iterations is not None:
            settings.max_replan_iterations = max(1, request.options.max_iterations)
        return settings

    def _replay_hours(self, request: RunRequest) -> list[str]:
        steps = max(1, request.options.replay_steps or 1)
        if steps == 1:
            return [request.input.replay_hour]
        try:
            start = datetime.fromisoformat(request.input.replay_hour.replace("Z", "+00:00"))
        except ValueError:
            return [request.input.replay_hour]
        return [
            (start + timedelta(hours=i)).astimezone(timezone.utc).isoformat(timespec="minutes").replace("+00:00", "Z")
            for i in range(steps)
        ]

    def _plan_one_hour(
        self,
        *,
        replay_hour: str,
        request: RunRequest,
        settings: Settings,
        analyst: Analyst,
        planner: Planner,
        simulator: Simulator,
        critic: Critic,
        risk: Risk,
        lessons: list[str],
    ) -> dict[str, Any]:
        analyst.gather(region=request.input.region, replay_hour=replay_hour, goal=request.input.goal)
        constraints = request.input.constraints.model_dump()

        plan, simulation, critique, iterations = self._refine(
            planner.propose(replay_hour=replay_hour, constraints=constraints, lessons=lessons),
            planner=planner,
            simulator=simulator,
            critic=critic,
            replay_hour=replay_hour,
            constraints=constraints,
            lessons=lessons,
            max_iterations=settings.max_replan_iterations,
        )
        escalations = risk.classify(plan, critique)
        return {
            "plan": plan,
            "simulation": simulation,
            "critique": critique,
            "iterations": iterations,
            "escalations": escalations,
        }

    def _refine(
        self,
        plan: Plan,
        *,
        planner: Planner,
        simulator: Simulator,
        critic: Critic,
        replay_hour: str,
        constraints: dict[str, Any],
        lessons: list[str],
        max_iterations: int,
    ) -> tuple[Plan, SimulationResult, Critique, int]:
        """Planner <-> Simulator <-> Critic, bounded."""
        iterations = 0
        simulation = simulator.run(plan, constraints)
        critique = critic.review(plan, simulation)

        while not critique.accepted and iterations + 1 < max_iterations:
            iterations += 1
            plan = planner.revise(
                plan, simulation, critique,
                replay_hour=replay_hour, constraints=constraints, lessons=lessons,
            )
            simulation = simulator.run(plan, constraints)
            critique = critic.review(plan, simulation)

        iterations += 1
        if not critique.accepted:
            planner.tracer.log(
                agent_name="Orchestrator",
                action="iteration_cap_reached",
                output_summary=(
                    f"Critic still refuses after {iterations} iterations; returning best plan with "
                    f"{len(critique.findings)} unresolved findings and escalating"
                ),
                status="unresolved",
                confidence=critique.confidence,
            )
        return plan, simulation, critique, iterations

    def _recall(
        self, memory: HMem, request: RunRequest, tracer: Tracer, world: WorldModel
    ) -> list[str]:
        tags = [request.input.scenario, *[c.lower() for c in request.input.region]]
        items = memory.recall(tags, limit=5)
        lessons = [item.statement for item in items]

        # A lesson about a shelter filling early becomes a derate the Planner
        # actually uses, not just a line in the prompt.
        derated: list[str] = []
        for item in items:
            if item.subject.startswith("shelter:") and "fill" in item.statement.lower():
                shelter_id = item.subject.split(":", 1)[1]
                world.put_entity(shelter_id, "shelter", memory_derate=MEMORY_DERATE)
                derated.append(shelter_id)

        tracer.engine_op(
            "hmem",
            "recall_memory",
            f"{len(lessons)} lessons recalled for tags {tags[:3]}"
            + (f"; derated shelters: {', '.join(derated)}" if derated else ""),
        )
        return lessons

    def _write_lessons(
        self,
        memory: HMem,
        request: RunRequest,
        plan: Plan,
        simulation: SimulationResult,
        critique: Critique,
        tracer: Tracer,
    ) -> None:
        tags = [request.input.scenario, *[c.lower() for c in request.input.region]]
        written = 0

        for violation in simulation.violations:
            if violation.code == "shelter_at_capacity" and violation.action_id:
                action = plan.by_id().get(violation.action_id)
                if action:
                    memory.remember(
                        f"shelter:{action.target}",
                        f"Shelter {action.target} was already full at {plan.replay_hour}; "
                        "it fills faster than its nominal headroom suggests",
                        level=LEVEL_EPISODE,
                        tags=tags,
                        confidence=0.75,
                        run_id=request.run_id,
                        evidence=[f"shelter:{action.target}:occupancy"],
                    )
                    written += 1

        if not critique.accepted:
            codes = sorted({f.code for f in critique.findings})
            memory.remember(
                f"planner:{request.input.scenario}",
                f"Plan at {plan.replay_hour} was refused for: {', '.join(codes[:4])}",
                level=LEVEL_EPISODE,
                tags=tags,
                confidence=0.7,
                run_id=request.run_id,
            )
            written += 1

        margin = simulation.metrics.get("capacity_margin")
        if margin is not None and margin < 0:
            memory.remember(
                f"capacity:{request.input.scenario}",
                f"Opening budget fell {abs(margin):.0f} spaces short at {plan.replay_hour}; "
                "start with a larger opening budget for this scenario",
                level=LEVEL_EPISODE,
                tags=tags,
                confidence=0.8,
                run_id=request.run_id,
            )
            written += 1

        promoted = memory.consolidate()
        memory.flush()
        tracer.engine_op(
            "hmem",
            "write_memory",
            f"{written} episodes written, {len(promoted)} promoted to lessons, store={memory.digest()}",
        )

    def _failure(
        self,
        request: RunRequest,
        tracer: Tracer,
        started: float,
        settings: Settings,
        error: StructuredError,
    ) -> RunResponse:
        tracer.log(
            agent_name="Orchestrator",
            action="run_failed",
            output_summary=f"{error.type}: {error.message}",
            status="error",
        )
        return RunResponse(
            run_id=request.run_id,
            status="failed",
            output=None,
            iterations=0,
            models_used=tracer.models_used,
            models_attempted=tracer.models_attempted,
            engines_used=sorted(set(tracer.engines_used)),
            zetaris_queries=tracer.zetaris_queries,
            trace_id=tracer.trace_id,
            log_file=str(tracer.log_file),
            execution_time_seconds=round(time.monotonic() - started, 3),
            sample_mode=settings.sample_mode,
            error=error,
        )
