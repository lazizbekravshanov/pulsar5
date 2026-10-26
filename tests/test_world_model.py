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
