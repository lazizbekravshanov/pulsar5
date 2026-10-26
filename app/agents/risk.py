"""Risk / Escalation (Fast lane) — the human gate.

Two things decide whether an action may auto-commit:

  * **Reversibility.** Staging supplies can be undone. An evacuation order
    cannot: people leave, roads load up, and the order carries legal weight.
    Irreversible actions always go to a human.
  * **Confidence.** Anything below `CONFIDENCE_ESCALATION_THRESHOLD` goes to a
    human regardless of kind.

The classification is rule-based on purpose. The Fast lane is asked to spot
*additional* irreversibility a rule might miss (an action whose wording implies
a public order, say), and may only ever escalate more, never less. A model
cannot talk this agent into auto-committing something.
"""

from __future__ import annotations

from app.agents.base import Agent
from app.schemas import Critique, Escalation, Plan

IRREVERSIBLE_KINDS = {"evacuate_zone"}
_IRREVERSIBLE_HINTS = ("evacuat", "mandatory", "order", "curfew", "close bridge", "shut")

_SYSTEM = """You classify emergency actions by reversibility for a human-approval gate.
An action is irreversible if undoing it is impossible or carries public/legal weight
(evacuation orders, mandatory closures, public directives). Opening a shelter or
staging supplies is reversible. Reply with JSON only:
{"irreversible_action_ids": ["ACT-001", ...], "why": "short reason"}"""


class Risk(Agent):
    name = "Risk"
    role = "Classifies actions reversible or irreversible and routes low-confidence or irreversible calls to a human"
    lane = "fast"

    def classify(self, plan: Plan, critique: Critique | None) -> list[Escalation]:
        threshold = self.settings.confidence_escalation_threshold
        plan_confidence = critique.confidence if critique else 0.5

        flagged = self._model_flags(plan)
        escalations: list[Escalation] = []

        for action in plan.actions:
            rule_irreversible = (
                action.kind in IRREVERSIBLE_KINDS
                or not action.reversible
                or any(hint in action.detail.lower() for hint in _IRREVERSIBLE_HINTS)
            )
            irreversible = rule_irreversible or action.action_id in flagged
            if irreversible != action.reversible:
                action.reversible = not irreversible

            effective_confidence = round(min(action.confidence, plan_confidence), 2)

            reasons: list[str] = []
            if irreversible:
                reasons.append("irreversible action requires human authorisation")
            if effective_confidence < threshold:
                reasons.append(
                    f"confidence {effective_confidence:.2f} is below the {threshold:.2f} threshold"
                )

            if reasons:
                escalations.append(
                    Escalation(
                        action_id=action.action_id,
                        reason="; ".join(reasons),
                        confidence=effective_confidence,
                        reversible=not irreversible,
                    )
                )
                self.world.set_decision_status(action.action_id, "escalated")
            else:
                self.world.set_decision_status(action.action_id, "committed")

        self.log(
            "classify_risk",
            input_summary=f"{len(plan.actions)} actions, plan confidence {plan_confidence:.2f}",
            output_summary=(
                f"{len(escalations)} actions held for human approval, "
                f"{len(plan.actions) - len(escalations)} auto-committed"
            ),
            confidence=plan_confidence,
            target_agent="Human",
            extra={"escalated_action_ids": [e.action_id for e in escalations], "threshold": threshold},
        )
        return escalations

    def _model_flags(self, plan: Plan) -> set[str]:
        """Extra irreversibility spotted by the Fast lane. Can only add."""
        view = [
            {"action_id": a.action_id, "kind": a.kind, "detail": a.detail[:160]} for a in plan.actions[:16]
        ]
        chat = self.ask(
            [
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": f"Actions:\n{view}"},
            ],
            action="classify_reversibility",
            max_tokens=500,
        )
        parsed = chat.json() if chat.ok else None
        if isinstance(parsed, dict) and isinstance(parsed.get("irreversible_action_ids"), list):
            known = {a.action_id for a in plan.actions}
            return {str(x) for x in parsed["irreversible_action_ids"] if str(x) in known}
        return set()
