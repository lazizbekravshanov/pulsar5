"""Shared plumbing for the six agents.

Every agent gets the same handles (settings, tracer, world model, adapters) and
declares its lane and role. Agents are specialised and hand off to each other;
none of them is a relabelled copy of another.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.adapters.nightshift import NightshiftClient
from app.adapters.zetaris import ZetarisClient
from app.config import Settings
from app.engines.world_model.engine import WorldModel
from app.schemas import AgentCard
from app.tracing import Tracer

if TYPE_CHECKING:  # pragma: no cover
    from app.engines.hmem.engine import HMem


@dataclass
class AgentContext:
    settings: Settings
    tracer: Tracer
    world: WorldModel
    zetaris: ZetarisClient
    nightshift: NightshiftClient
    memory: HMem


class Agent:
    name: str = "Agent"
    role: str = ""
    lane: str = "fast"

    def __init__(self, ctx: AgentContext) -> None:
        self.ctx = ctx

    @property
    def card(self) -> AgentCard:
        return AgentCard(name=self.name, role=self.role, lane=self.lane)

    # convenience accessors used all over the agents
    @property
    def world(self) -> WorldModel:
        return self.ctx.world

    @property
    def tracer(self) -> Tracer:
        return self.ctx.tracer

    @property
    def settings(self) -> Settings:
        return self.ctx.settings

    def log(self, action: str, **kwargs: object) -> None:
        self.tracer.log(agent_name=self.name, action=action, **kwargs)  # type: ignore[arg-type]

    def ask(
        self,
        messages: list[dict[str, str]],
        *,
        action: str,
        temperature: float = 0.1,
        max_tokens: int = 1200,
    ):
        return self.ctx.nightshift.chat(
            self.lane,
            messages,
            agent_name=self.name,
            action=action,
            temperature=temperature,
            max_tokens=max_tokens,
        )
