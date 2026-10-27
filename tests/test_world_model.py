"""World Model engine conformance: shared state that parallel agents cannot corrupt."""

import threading

from app.engines.world_model.engine import Provenance, WorldModel


def _prov(as_of="2022-09-26T12:00Z", table="shelters"):
    return Provenance(source_table=table, query="SELECT 1", as_of=as_of)


def test_concurrent_writes_do_not_lose_updates():
    """Eight agents writing at once; every distinct fact must survive."""
    world = WorldModel()
    writers, per_writer = 8, 60

    def write(agent):
        for i in range(per_writer):
            world.assert_belief(f"f-{agent}-{i}", f"s-{agent}", "p", i, _prov(), written_by=f"agent{agent}")

    threads = [threading.Thread(target=write, args=(n,)) for n in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(world.beliefs()) == writers * per_writer
    assert world.write_count == writers * per_writer
    for agent in range(writers):
        assert len(world.beliefs(subject=f"s-{agent}")) == per_writer


def test_concurrent_entity_writes_merge_rather_than_overwrite():
    """Two agents enriching the same entity must both be visible."""
    world = WorldModel()
    world.put_entity("S-1", "shelter", capacity=1000)

    def add_occupancy():
        for _ in range(200):
            world.put_entity("S-1", "shelter", current_occupancy=250)

    def add_location():
        for _ in range(200):
            world.put_entity("S-1", "shelter", lat=26.6, lon=-81.8)

    threads = [threading.Thread(target=add_occupancy), threading.Thread(target=add_location)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    shelter = world.entity("S-1")
    assert shelter["capacity"] == 1000
    assert shelter["current_occupancy"] == 250
    assert shelter["lat"] == 26.6


def test_stale_write_does_not_clobber_a_fresher_fact():
    world = WorldModel()
    world.assert_belief("x", "shelter:S-1", "occupancy", 900, _prov("2022-09-27T00:00Z"))
    world.assert_belief("x", "shelter:S-1", "occupancy", 10, _prov("2022-09-26T00:00Z"))

    assert world.belief_value("x") == 900
    conflicts = world.conflicts()
    assert len(conflicts) == 1
    assert conflicts[0]["reason"] == "stale_write"


def test_belief_carries_provenance_and_versions():
    world = WorldModel()
    world.assert_belief("f1", "s", "p", 1, _prov("2022-09-26T00:00Z"), written_by="Analyst")
    belief, deltas = world.assert_belief("f1", "s", "p", 2, _prov("2022-09-26T06:00Z"), written_by="Reviser")

    assert belief.version == 2
    assert belief.written_by == "Reviser"
    assert belief.provenance.source_table == "shelters"
    assert {d.field_name for d in deltas} == {"value", "as_of"}


def test_decision_graph_finds_only_dependents():
    world = WorldModel()
    world.assert_belief("fact-a", "s", "p", 1, _prov())
    world.assert_belief("fact-b", "s", "p", 2, _prov())
    world.record_decision("d-a", "evacuate_zone", "Z1", "", depends_on=["fact-a"])
    world.record_decision("d-b", "stage_supplies", "ST1", "", depends_on=["fact-b"])
    world.record_decision("d-ab", "open_shelter", "S1", "", depends_on=["fact-a", "fact-b"])

    hit = {d.decision_id for d in world.depends_on_any(["fact-a"])}
    assert hit == {"d-a", "d-ab"}

    world.invalidate(hit, "fact-a changed")
    assert {d.decision_id for d in world.decisions(status="invalidated")} == {"d-a", "d-ab"}
    assert {d.decision_id for d in world.decisions(status="active")} == {"d-b"}


def test_stale_beliefs_reports_against_a_cutoff():
    world = WorldModel()
    world.assert_belief("old", "s", "p", 1, _prov("2022-09-25T00:00Z"))
    world.assert_belief("new", "s", "p", 2, _prov("2022-09-27T00:00Z"))

    stale = {b.fact_id for b in world.stale_beliefs("2022-09-26T00:00Z")}
    assert stale == {"old"}


def test_snapshot_round_trips_through_from_snapshot():
    """A world rebuilt from its snapshot is the same world: typed beliefs with
    provenance, decisions with status and reason, conflicts and the revision."""
    world = WorldModel()
    world.put_entity("S-1", "shelter", capacity=1000, county="Lee")
    world.put_entity("Z-9", "zone", population=5000)
    world.relate("Z-9", "nearest_shelter", "S-1")
    world.assert_belief("shelter:S-1:occupancy", "shelter:S-1", "current_occupancy", 100,
                        _prov("2022-09-26T00:00Z"), written_by="Analyst")
    world.assert_belief("shelter:S-1:occupancy", "shelter:S-1", "current_occupancy", 900,
                        _prov("2022-09-27T00:00Z"), written_by="Reviser")
    world.assert_belief("shelter:S-1:occupancy", "shelter:S-1", "current_occupancy", 10,
                        _prov("2022-09-25T00:00Z"), written_by="Late")
    world.record_decision("ACT-1", "open_shelter", "S-1", "open it", depends_on=["shelter:S-1:occupancy"],
                          confidence=0.8)
    world.record_decision("ACT-2", "evacuate_zone", "Z-9", "", depends_on=["zone:Z-9:population"])
    world.set_decision_status("ACT-1", "committed")
    world.set_decision_status("ACT-2", "invalidated", reason="declined by op1: no staff")

    rebuilt = WorldModel.from_snapshot(world.snapshot())

    assert rebuilt.snapshot() == world.snapshot()
    assert rebuilt.revision == world.revision
    belief = rebuilt.belief("shelter:S-1:occupancy")
    assert belief.version == 2 and belief.written_by == "Reviser"
    assert belief.provenance.source_table == "shelters" and belief.provenance.as_of == "2022-09-27T00:00Z"
    assert rebuilt.conflicts()[0]["reason"] == "stale_write"
    decision = rebuilt.decisions(status="invalidated")[0]
    assert decision.decision_id == "ACT-2" and decision.invalidated_reason == "declined by op1: no staff"
    assert rebuilt.decisions(status="committed")[0].depends_on == ("shelter:S-1:occupancy",)
    assert rebuilt.related("Z-9", "nearest_shelter") == ["S-1"]
    assert rebuilt.next_decision_number() == 2
    assert {d.decision_id for d in rebuilt.depends_on_any(["shelter:S-1:occupancy"])} == {"ACT-1"}


def test_from_snapshot_does_not_share_state_with_its_source():
    snapshot = WorldModel().snapshot()
    snapshot["entities"]["S-1"] = {"entity_id": "S-1", "kind": "shelter"}
    rebuilt = WorldModel.from_snapshot(snapshot)
    rebuilt.put_entity("S-2", "shelter")

    assert "S-2" not in snapshot["entities"]
    assert rebuilt.entity("S-1") == {"entity_id": "S-1", "kind": "shelter"}
