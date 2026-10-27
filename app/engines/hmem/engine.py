"""Meterless H-MEM engine — "give my agent memory".

Hierarchical memory that survives across runs, on three levels:

    L0 episodes  one observation from one run ("S-14 hit capacity at hour 9")
    L1 lessons   a pattern seen in 2+ episodes, promoted by `consolidate()`
    L2 standing  operator preferences and durable principles, never expire

Recall is tag-scored, not a vector search: a hurricane EOC run has a handful of
tags (scenario, county, shelter id) and exact overlap is both cheaper and
auditable, which matters when a judge asks *why* a memory surfaced.

The store is a single JSON file under `data/hmem/`, so "run 2 remembers run 1"
is literally a fresh process reading the same file.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.config import HMEM_DIR
from app.schemas import utc_now_iso

ENGINE_NAME = "hmem"

LEVEL_EPISODE = "L0_episode"
LEVEL_LESSON = "L1_lesson"
LEVEL_STANDING = "L2_standing"

_PROMOTE_AFTER = 2  # identical-subject episodes needed before a lesson is formed


@dataclass
class MemoryItem:
    memory_id: str
    level: str
    subject: str
    statement: str
    tags: list[str] = field(default_factory=list)
    confidence: float = 0.6
    run_id: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    evidence: list[str] = field(default_factory=list)
    hits: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class HMem:
    def __init__(self, store_path: Path | None = None) -> None:
        self.store_path = store_path or (HMEM_DIR / "store.json")
        self._lock = threading.RLock()
        self._items: dict[str, MemoryItem] = {}
        self._load()

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        if not self.store_path.exists():
            return
        try:
            blob = json.loads(self.store_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return
        for raw in blob.get("items", []):
            try:
                item = MemoryItem(**raw)
            except TypeError:
                continue
            self._items[item.memory_id] = item

    def flush(self) -> Path:
        with self._lock:
            payload = {
                "updated_at": utc_now_iso(),
                "items": [item.to_dict() for item in self._items.values()],
            }
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.store_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.store_path)
            return self.store_path

    # ----------------------------------------------------------------- writing

    def remember(
        self,
        subject: str,
        statement: str,
        *,
        level: str = LEVEL_EPISODE,
        tags: Iterable[str] = (),
        confidence: float = 0.6,
        run_id: str | None = None,
        evidence: Iterable[str] = (),
    ) -> MemoryItem:
        with self._lock:
            tag_list = sorted({t.lower() for t in tags if t})
            memory_id = f"{level}:{subject}:{len(self._items)}"
            item = MemoryItem(
                memory_id=memory_id,
                level=level,
                subject=subject,
                statement=statement,
                tags=tag_list,
                confidence=confidence,
                run_id=run_id,
                evidence=list(evidence),
            )
            self._items[memory_id] = item
            return item

    def consolidate(self) -> list[MemoryItem]:
        """Promote repeated episodes about the same subject into an L1 lesson."""
        with self._lock:
            by_subject: dict[str, list[MemoryItem]] = {}
            for item in self._items.values():
                if item.level == LEVEL_EPISODE:
                    by_subject.setdefault(item.subject, []).append(item)

            promoted: list[MemoryItem] = []
            for subject, episodes in by_subject.items():
                if len(episodes) < _PROMOTE_AFTER:
                    continue
                already = any(
                    i.level == LEVEL_LESSON and i.subject == subject for i in self._items.values()
                )
                if already:
                    continue
                runs = sorted({e.run_id for e in episodes if e.run_id})
                tags = sorted({t for e in episodes for t in e.tags})
                lesson = self.remember(
                    subject,
                    f"Seen in {len(episodes)} runs: {episodes[-1].statement}",
                    level=LEVEL_LESSON,
                    tags=tags,
                    confidence=min(0.95, 0.6 + 0.1 * len(episodes)),
                    evidence=[e.memory_id for e in episodes],
                )
                lesson.run_id = runs[-1] if runs else None
                promoted.append(lesson)
            return promoted

    # ----------------------------------------------------------------- reading

    def recall(self, tags: Iterable[str], *, limit: int = 5, min_level: str | None = None) -> list[MemoryItem]:
        """Rank by tag overlap, then level (standing > lesson > episode), then confidence."""
        wanted = {t.lower() for t in tags if t}
        level_rank = {LEVEL_STANDING: 2, LEVEL_LESSON: 1, LEVEL_EPISODE: 0}

        with self._lock:
            candidates = list(self._items.values())

        scored: list[tuple[int, int, float, MemoryItem]] = []
        for item in candidates:
            if min_level and level_rank.get(item.level, 0) < level_rank.get(min_level, 0):
                continue
            overlap = len(wanted & set(item.tags))
            if overlap == 0 and item.level != LEVEL_STANDING:
                continue
            scored.append((overlap, level_rank.get(item.level, 0), item.confidence, item))

        scored.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
        top = [row[3] for row in scored[:limit]]
        with self._lock:
            for item in top:
                item.hits += 1
        return top

    def all_items(self) -> list[MemoryItem]:
        with self._lock:
            return list(self._items.values())

    def digest(self) -> dict[str, int]:
        with self._lock:
            return {
                "total": len(self._items),
                "episodes": sum(1 for i in self._items.values() if i.level == LEVEL_EPISODE),
                "lessons": sum(1 for i in self._items.values() if i.level == LEVEL_LESSON),
                "standing": sum(1 for i in self._items.values() if i.level == LEVEL_STANDING),
            }
