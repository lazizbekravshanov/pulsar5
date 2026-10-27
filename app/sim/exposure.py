"""Per-zone cone membership as a belief of its own.

An evacuation order depends on whether its zone sits inside the forecast
cone, not on the polygon's coordinates. Deriving `zone:<id>:in_cone` from the
cone belief gives the decision graph something precise to depend on: when the
track moves, only the zones whose membership flipped produce a changed belief,
and only the decisions that cited them are revisited.
"""

from __future__ import annotations

from typing import Any

from app.engines.world_model.engine import BeliefDelta, Provenance, WorldModel
from app.schemas import utc_now_iso
from app.sim.geo import point_in_polygon


def derive_zone_exposure(
    world: WorldModel, cone: Any, provenance: Provenance, *, written_by: str
) -> list[BeliefDelta]:
    """Assert `zone:<id>:in_cone` for every zone entity against this cone.

    Returns the deltas, so a caller can tell which zones actually changed
    sides. A zone without coordinates, or an empty cone, asserts nothing.
    """
    points = [(float(p[0]), float(p[1])) for p in (cone or []) if isinstance(p, list | tuple) and len(p) >= 2]
    if len(points) < 3:
        return []
    derived = Provenance(
        source_table=f"derived from {provenance.source_table}",
        query=None,
        as_of=provenance.as_of,
        retrieved_at=utc_now_iso(),
        data_source="derived",
    )
    deltas: list[BeliefDelta] = []
    for zone in world.entities("zone"):
        lat, lon = zone.get("lat"), zone.get("lon")
        if lat is None or lon is None:
            continue
        exposed = point_in_polygon((float(lat), float(lon)), points)
        zone_id = zone["entity_id"]
        _, item_deltas = world.assert_belief(
            f"zone:{zone_id}:in_cone", f"zone:{zone_id}", "in_cone", exposed, derived, written_by=written_by
        )
        deltas.extend(item_deltas)
    return deltas
