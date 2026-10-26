"""One definition of usable shelter capacity, shared by Planner and Simulator.

If these two disagreed, an H-MEM lesson would be decoration: the Planner would
quietly treat a shelter as smaller while the Simulator kept reporting its
nominal headroom, and the capacity check would pass on numbers nobody planned
against. Both read this function instead.
"""

from __future__ import annotations

from typing import Any


def usable_headroom(shelter: dict[str, Any]) -> float:
    """Spaces this shelter can actually absorb.

    `memory_derate` is written by the orchestrator when H-MEM recalls that the
    shelter filled faster than its headroom suggested in an earlier run. It is
    the mechanism by which a lesson from run 1 changes run 2's plan.
    """
    capacity = float(shelter.get("capacity") or 0)
    occupancy = float(shelter.get("current_occupancy") or 0)
    derate = float(shelter.get("memory_derate") or 1.0)
    return max(0.0, (capacity - occupancy) * derate)


def is_usable(shelter: dict[str, Any]) -> bool:
    return shelter.get("status") != "closed" and usable_headroom(shelter) > 0
