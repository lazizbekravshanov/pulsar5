"""The reasoning loop: critique, revision, bounded iteration, escalation."""

import json
from pathlib import Path

import pytest

from app.adapters.nightshift import NightshiftClient
from app.adapters.zetaris import ZetarisClient
from app.agents.analyst import Analyst
from app.agents.base import AgentContext
from app.agents.critic import Critic
from app.agents.planner import Planner
from app.agents.risk import Risk
from app.agents.simulator import Simulator
from app.config import load_settings
from app.engines.hmem.engine import HMem
from app.engines.world_model.engine import WorldModel
from app.orchestrator import Orchestrator
from app.schemas import Critique, RunRequest
from app.tracing import Tracer

REPO = Path(__file__).resolve().parent.parent


@pytest.fixture
def ctx(tmp_path):
    settings = load_settings()
    settings.sample_mode = True
    settings.max_replan_iterations = 3
    tracer = Tracer("test-run", secrets=settings.secrets(), logs_dir=tmp_path, echo=False)
    world = WorldModel()
    return AgentContext(
        settings=settings,
        tracer=tracer,
        world=world,
        zetaris=ZetarisClient(settings, tracer),
        nightshift=NightshiftClient(settings, tracer),
        memory=HMem(tmp_path / "hmem.json"),
    )


@pytest.fixture
def loaded(ctx):
    """A world loaded from the recorded Zetaris results."""
    Analyst(ctx).gather(
        region=["Lee", "Charlotte", "Sarasota", "Pinellas", "Hillsborough"],
        replay_hour="2022-09-26T12:00Z",
        goal="test",
    )
    return ctx


def test_analyst_loads_facts_with_provenance(loaded):
    world = loaded.world
    assert world.entities("zone"), "no zones loaded"
    assert world.entities("shelter"), "no shelters loaded"

    belief = world.beliefs(predicate="capacity")[0]
    assert belief.provenance.source_table == "shelters"
    assert belief.provenance.query and "pulsar5.shelters" in belief.provenance.query
    assert belief.provenance.data_source == "sample_cache"
    assert belief.written_by == "Analyst"


def test_every_action_cites_evidence_that_exists(loaded):
    plan = Planner(loaded).propose(
        replay_hour="2022-09-26T12:00Z", constraints={"supply_trucks": 12, "staging_sites_max": 4}
    )
    for action in plan.actions:
        if action.kind == "hold":
            continue
        assert action.evidence_ids, f"{action.action_id} cites nothing"
        for fact_id in action.evidence_ids:
            assert loaded.world.belief(fact_id) is not None, f"{fact_id} does not exist"


def test_critic_rejection_triggers_a_revision_that_improves_the_plan(loaded):
    constraints = {"supply_trucks": 12, "staging_sites_max": 4}
    planner, simulator, critic = Planner(loaded), Simulator(loaded), Critic(loaded)

    first = planner.propose(replay_hour="2022-09-26T12:00Z", constraints=constraints)
    first_sim = simulator.run(first, constraints)
    first_critique = critic.review(first, first_sim)

    assert not first_critique.accepted, "expected the budgeted first pass to be rejected"
    assert first_critique.instructions, "a rejection must carry refinement instructions"
    assert first_sim.metrics["capacity_margin"] < 0

    revised = planner.revise(
        first, first_sim, first_critique,
        replay_hour="2022-09-26T12:00Z", constraints=constraints, lessons=[],
    )
    revised_sim = simulator.run(revised, constraints)

    assert revised_sim.metrics["capacity_margin"] > first_sim.metrics["capacity_margin"]
    assert critic.review(revised, revised_sim).accepted


def test_loop_stops_at_max_iterations_and_escalates(loaded):
    """With a cap of 1 the loop cannot revise, and the run still returns."""
    constraints = {"supply_trucks": 12, "staging_sites_max": 4}
    orchestrator = Orchestrator(loaded.settings)
    planner, simulator, critic = Planner(loaded), Simulator(loaded), Critic(loaded)

    plan, simulation, critique, iterations = orchestrator._refine(
        planner.propose(replay_hour="2022-09-26T12:00Z", constraints=constraints),
        planner=planner, simulator=simulator, critic=critic,
        replay_hour="2022-09-26T12:00Z", constraints=constraints, lessons=[], max_iterations=1,
    )

    assert iterations == 1
    assert not critique.accepted
    assert plan.actions, "a capped loop still returns its best plan"
    assert critique.findings, "unresolved findings must travel with it"


def test_irreversible_action_goes_to_the_human_queue(loaded):
    constraints = {"supply_trucks": 12, "staging_sites_max": 4}
    plan = Planner(loaded).propose(replay_hour="2022-09-26T12:00Z", constraints=constraints)
    confident = Critique(accepted=True, confidence=0.95)

    escalations = Risk(loaded).classify(plan, confident)
    escalated = {e.action_id for e in escalations}

    evacuations = [a for a in plan.actions if a.kind == "evacuate_zone"]
    assert evacuations, "fixture should produce evacuations"
    for action in evacuations:
        assert action.action_id in escalated, "an evacuation order must never auto-commit"
        assert action.reversible is False

    reversible = [a for a in plan.actions if a.kind in {"open_shelter", "stage_supplies"}]
    assert reversible, "fixture should produce reversible actions"
    assert any(a.action_id not in escalated for a in reversible), (
        "with high confidence some reversible action should auto-commit"
    )


def test_low_confidence_escalates_even_a_reversible_action(loaded):
    constraints = {"supply_trucks": 12, "staging_sites_max": 4}
    plan = Planner(loaded).propose(replay_hour="2022-09-26T12:00Z", constraints=constraints)
    doubtful = Critique(accepted=True, confidence=0.2)

    escalations = Risk(loaded).classify(plan, doubtful)
    assert {e.action_id for e in escalations} == {a.action_id for a in plan.actions}
    assert all("below the" in e.reason for e in escalations)


def test_committed_and_escalated_decisions_are_recorded_in_the_world_model(loaded):
    constraints = {"supply_trucks": 12, "staging_sites_max": 4}
    plan = Planner(loaded).propose(replay_hour="2022-09-26T12:00Z", constraints=constraints)
    Risk(loaded).classify(plan, Critique(accepted=True, confidence=0.95))

    statuses = {d.decision_id: d.status for d in loaded.world.decisions()}
    assert set(statuses.values()) <= {"committed", "escalated", "active", "invalidated"}
    assert "escalated" in statuses.values()
    assert "committed" in statuses.values()
