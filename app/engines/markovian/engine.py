"""Meterless Markovian engine — "long tasks, flat context".

The Ian replay is a long task: 24+ hourly steps. A naive loop appends each
hour's findings to a growing prompt and the context blows up. Here each step is
bounded and Markovian: step N sees only

    * the step's own payload, and
    * a compressed carryover from step N-1 (hard caps on fields, list lengths
      and total characters),

so context stays flat whether the replay is 4 hours or 240. H-MEM context is
injected once, at step 1 only, per the official COMBINE prompt — later steps
inherit it through the carryover, never by re-injection.

`context_sizes` is what the conformance test and the demo scoreboard read.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

ENGINE_NAME = "markovian"

MAX_CARRYOVER_CHARS = 1800
MAX_LIST_ITEMS = 6
MAX_NOTE_CHARS = 160


def compress(carryover: dict[str, Any]) -> dict[str, Any]:
    """Shrink a carryover to a bounded shape. Pure function, easy to test."""
    out: dict[str, Any] = {}
    for key, value in carryover.items():
        if isinstance(value, list):
            trimmed = value[-MAX_LIST_ITEMS:]
            out[key] = [
                (v[:MAX_NOTE_CHARS] if isinstance(v, str) else v) for v in trimmed
            ]
        elif isinstance(value, str):
            out[key] = value[:MAX_NOTE_CHARS]
        elif isinstance(value, dict):
            # keep only scalar leaves of a nested dict (digests look like this)
            out[key] = {
                k: v for k, v in list(value.items())[:MAX_LIST_ITEMS] if isinstance(v, (int, float, str, bool))
            }
        else:
            out[key] = value

    # Final belt-and-braces cap: drop the least important keys until it fits.
    priority = ["step", "replay_hour", "digest", "open_issues", "notes", "memory"]
    while len(json.dumps(out, default=str)) > MAX_CARRYOVER_CHARS:
        droppable = [k for k in out if k not in priority[:3]]
        if not droppable:
            break
        out.pop(sorted(droppable, key=lambda k: priority.index(k) if k in priority else 99)[-1])
    return out


@dataclass
class StepRecord:
    index: int
    label: str
    context_chars: int
    carryover_chars: int
    output_summary: str = ""


@dataclass
class MarkovianRunner:
    """Drives a bounded-step loop and keeps the per-step context measurements."""

    steps: list[StepRecord] = field(default_factory=list)

    def run(
        self,
        labels: list[str],
        step_fn: Callable[[int, str, dict[str, Any]], dict[str, Any]],
        *,
        initial_memory: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """`step_fn(index, label, context) -> carryover` for each label in order.

        `initial_memory` (H-MEM recall) is placed in the step-1 context only.
        """
        carryover: dict[str, Any] = {}
        for index, label in enumerate(labels, start=1):
            context: dict[str, Any] = {"step": index, "replay_hour": label, "carryover": carryover}
            if index == 1 and initial_memory:
                context["memory"] = initial_memory  # COMBINE: inject once, at step 1

            context_chars = len(json.dumps(context, default=str))
            raw_carryover = step_fn(index, label, context) or {}
            carryover = compress({**raw_carryover, "step": index, "replay_hour": label})

            self.steps.append(
                StepRecord(
                    index=index,
                    label=label,
                    context_chars=context_chars,
                    carryover_chars=len(json.dumps(carryover, default=str)),
                    output_summary=str(raw_carryover.get("summary", ""))[:MAX_NOTE_CHARS],
                )
            )
        return carryover

    # --------------------------------------------------------------- reporting

    @property
    def context_sizes(self) -> list[int]:
        return [s.context_chars for s in self.steps]

    def bounded(self, *, tolerance: float = 1.6) -> bool:
        """True when later steps are not materially larger than early ones.

        Compares the max context after the first step (step 1 carries the
        one-time H-MEM injection) against the first post-injection step.
        """
        sizes = self.context_sizes
        if len(sizes) < 3:
            return True
        baseline = sizes[1]
        return max(sizes[1:]) <= baseline * tolerance

    def digest(self) -> dict[str, Any]:
        sizes = self.context_sizes
        return {
            "steps": len(self.steps),
            "max_context_chars": max(sizes) if sizes else 0,
            "min_context_chars": min(sizes) if sizes else 0,
            "mean_context_chars": round(sum(sizes) / len(sizes)) if sizes else 0,
            "bounded": self.bounded(),
            "carryover_cap_chars": MAX_CARRYOVER_CHARS,
        }
