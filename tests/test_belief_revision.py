"""Change of mind: only decisions that depended on changed facts may fall."""


import pytest

from app.adapters.nightshift import NightshiftClient
from app.adapters.zetaris import ZetarisClient
from app.agents.analyst import Analyst
from app.agents.base import AgentContext
from app.agents.belief_reviser import BeliefReviser, diff_plans
from app.agents.planner import Planner
from app.config import load_settings
from app.engines.hmem.engine import HMem
from app.engines.world_model.engine import Provenance, WorldModel
from app.schemas import Event
from app.tracing import Tracer

FORT_MYERS_CONE = [
    [27.45, -82.75], [27.45, -81.55], [26.60, -81.40],
    [26.10, -81.70], [26.15, -82.40], [26.90, -82.80],
]


@pytest.fixture
def ctx(tmp_path):
    settings = load_settings()
    settings.sample_mode = True
    tracer = Tracer("test-revision", secrets=settings.secrets(), logs_dir=tmp_path, echo=False)
    world = WorldModel()
    return AgentContext(
        settings=settings, tracer=tracer, world=world,
        zetaris=ZetarisClient(settings, tracer),
        nightshift=NightshiftClient(settings, tracer),
        memory=HMem(tmp_path / "hmem.json"),
    )


def test_invalidates_only_decisions_that_depended_on_changed_facts(ctx):
    world = ctx.world
    prov = Provenance(source_table="shelters", as_of="2022-09-26T00:00Z")
    world.assert_belief("shelter:S-1:occupancy", "shelter:S-1", "current_occupancy", 100, prov)
    world.assert_belief("zone:Z-9:population", "zone:Z-9", "population", 5000, prov)
    world.put_entity("S-1", "shelter", capacity=1000, current_occupancy=100)

    world.record_decision("d-shelter", "open_shelter", "S-1", "", depends_on=["shelter:S-1:occupancy"])
    world.record_decision("d-zone", "evacuate_zone", "Z-9", "", depends_on=["zone:Z-9:population"])

    result = BeliefReviser(ctx).apply_event(
        Event(
            type="shelter_capacity",
            payload={"shelter_id": "S-1", "capacity": 1000, "current_occupancy": 1000,
                     "status": "at_capacity", "updated_at": "2022-09-27T00:00Z"},
        )
    )

    assert result["invalidated_decisions"] == ["d-shelter"]
    assert "d-zone" in result["preserved_decisions"]

    changed = {c.fact_id for c in result["changed_beliefs"]}
    assert "shelter:S-1:occupancy" in changed
    # The payload also carries capacity, which the fixture never asserted, so it
    # is written as a new belief. No zone fact may appear.
    assert not any(fact_id.startswith("zone:") for fact_id in changed)


def test_unrelated_event_invalidates_nothing(ctx):
    world = ctx.world
    prov = Provenance(source_table="shelters", as_of="2022-09-26T00:00Z")
    world.assert_belief("shelter:S-1:occupancy", "shelter:S-1", "current_occupancy", 100, prov)
    world.record_decision("d-shelter", "open_shelter", "S-1", "", depends_on=["shelter:S-1:occupancy"])

    result = BeliefReviser(ctx).apply_event(
        Event(type="road_closure", payload={"event_id": "RD-77", "route": "SR-80", "status": "closed"})
    )

    assert result["invalidated_decisions"] == []
    assert "d-shelter" in result["preserved_decisions"]


def test_a_track_shift_moves_the_evacuation_footprint_south(ctx):
    """The headline behaviour: Tampa out, Lee in, Sarasota preserved."""
    Analyst(ctx).gather(
        region=["Lee", "Charlotte", "Sarasota", "Pinellas", "Hillsborough"],
        replay_hour="2022-09-26T12:00Z",
        goal="test",
    )
    constraints = {"supply_trucks": 12, "staging_sites_max": 4}
    planner = Planner(ctx)
    before = planner.propose(replay_hour="2022-09-26T12:00Z", constraints=constraints)

    def counties(plan):
        return {a.target.split("-")[1] for a in plan.actions if a.kind == "evacuate_zone"}

    before_counties = counties(before)
    assert "057" in before_counties or "103" in before_counties, "Tampa Bay expected under advisory 15"
    assert "071" not in before_counties, "Lee should be outside the early cone"

    result = BeliefReviser(ctx).apply_event(
        Event(
            type="advisory_update",
            payload={
                "advisory_number": 23, "issued_at": "2022-09-27T21:00Z",
                "center_lat": 25.4, "center_lon": -82.9, "max_wind_kt": 130,
                "forecast_landfall_county": "Lee", "cone_polygon": FORT_MYERS_CONE,
            },
        )
    )
    assert result["invalidated_decisions"], "a track shift must invalidate the decisions that cited the old cone"

    after = planner.propose(replay_hour="2022-09-27T21:00Z", constraints=constraints)
    after_counties = counties(after)

    assert "071" in after_counties, "Lee County must enter the plan after the southward shift"
    assert len(after_counties - before_counties) > 0, "the footprint must actually change"


def test_diff_matches_actions_on_kind_and_target_not_id():
    from app.schemas import Plan, PlanAction

    before = Plan(plan_id="a", replay_hour="h", actions=[
        PlanAction(action_id="ACT-001", kind="evacuate_zone", target="Z1", detail="d", priority=1),
        PlanAction(action_id="ACT-002", kind="open_shelter", target="S1", detail="d"),
    ])
    after = Plan(plan_id="b", replay_hour="h", actions=[
        PlanAction(action_id="ACT-900", kind="evacuate_zone", target="Z1", detail="d", priority=4),
        PlanAction(action_id="ACT-901", kind="open_shelter", target="S2", detail="d"),
    ])

    diff = diff_plans(before, after)
    assert [a.target for a in diff.added] == ["S2"]
    assert diff.removed == ["ACT-002"]
    # Same real-world action, new id: reported as changed, not added+removed.
    assert any(c["field"] == "priority" and c["before"] == 1 and c["after"] == 4 for c in diff.changed)
