"""Simulator (code, with a Fast-lane narration pass).

The verdict is deterministic code — `app/sim/checks.simulate` — so a plan
either breaks a constraint or it does not, regardless of what any model says.
The Fast lane is used only to turn the structured violations into one readable
line for the trace and the UI. If the model is unavailable the narration falls
back to a formatted string; the verdict is unchanged either way.
"""

from __future__ import annotations

from typing import Any

from app.agents.base import Agent
from app.schemas import Plan, SimulationResult
from app.sim.checks import simulate

_SYSTEM = """You summarise simulation results for an emergency operations log.
One sentence, plain words, no speculation beyond the data. Reply with JSON only:
{"summary": "one sentence"}"""


class Simulator(Agent):
    name = "Simulator"
    role = "Deterministic capacity, coverage, distance and budget checks; returns structured violations"
    lane = "fast"

    def run(self, plan: Plan, constraints: dict[str, Any]) -> SimulationResult:
        result = simulate(plan, self.world, constraints)

        high = [v for v in result.violations if v.severity == "high"]
        self.log(
            "simulate_plan",
            engine="world_model",
            input_summary=f"{len(plan.actions)} actions",
            output_summary=self._narrate(result),
            status="success" if result.ok else "violations_found",
            extra={
                "ok": result.ok,
                "violation_count": len(result.violations),
                "high_severity_count": len(high),
                "violation_codes": sorted({v.code for v in result.violations}),
                "metrics": result.metrics,
            },
        )
        self.ctx.tracer.handoff(self.name, "Critic", f"{len(result.violations)} violations")
        return result

    def _narrate(self, result: SimulationResult) -> str:
        deterministic = (
            f"{'passed' if result.ok else 'failed'}: {len(result.violations)} violations; "
            f"coverage {result.metrics.get('coverage_rate', 0):.0%}, "
            f"capacity margin {result.metrics.get('capacity_margin', 0):.0f}"
        )
        if not result.violations:
            return deterministic

        codes = ", ".join(sorted({v.code for v in result.violations}))
        chat = self.ask(
            [
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Violations: {codes}\nMetrics: {result.metrics}\n"
                        f"Worst message: {result.violations[0].message}"
                    ),
                },
            ],
            action="narrate_simulation",
            max_tokens=200,
        )
        parsed = chat.json() if chat.ok else None
        if isinstance(parsed, dict) and parsed.get("summary"):
            return f"{deterministic} :: {str(parsed['summary'])[:220]}"
        return f"{deterministic} :: {result.violations[0].message}"
