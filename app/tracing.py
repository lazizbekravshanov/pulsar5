"""JSONL tracing. One file per run, one JSON object per line.

Judges read these files, so every agent step, handoff, critique, rejection,
retry, fallback, Zetaris query, engine operation and model name lands here.
Secrets are redacted on the way in, not on the way out.
"""

from __future__ import annotations

import json
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Iterable

from app.config import LOGS_DIR
from app.schemas import utc_now_iso

_MASK = "...redacted"


def redact(text: str, secrets: Iterable[str]) -> str:
    """Replace every known secret with a marker. Short values are skipped so a
    one-character secret cannot blank out the whole line."""
    out = text
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, _MASK)
    return out


def _truncate(value: Any, limit: int = 600) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"...[+{len(value) - limit} chars]"
    return value


class Tracer:
    """Append-only JSONL writer, safe to share across threads."""

    def __init__(
        self,
        run_id: str,
        *,
        secrets: Iterable[str] = (),
        logs_dir: Path | None = None,
        echo: bool = True,
    ) -> None:
        self.run_id = run_id
        self.trace_id = str(uuid.uuid4())
        self._secrets = [s for s in secrets if s]
        self._lock = threading.Lock()
        self._echo = echo
        self._dir = logs_dir or LOGS_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        safe_run_id = "".join(c if c.isalnum() or c in "-_." else "_" for c in run_id)
        self.log_file = self._dir / f"trace-{safe_run_id}.jsonl"
        # One file per run. Re-running the same run_id replaces its trace rather
        # than appending to it, so a judge reading trace-<run_id>.jsonl sees one
        # run from start to finish and not every run that ever used that id.
        self.log_file.write_text("", encoding="utf-8")
        self.events: list[dict[str, Any]] = []
        self.models_used: list[str] = []
        self.engines_used: list[str] = []
        self.zetaris_queries = 0

    # ---------------------------------------------------------------- core

    def log(
        self,
        *,
        agent_name: str,
        action: str,
        input_summary: str = "",
        output_summary: str = "",
        target_agent: str | None = None,
        model: str | None = None,
        engine: str | None = None,
        zetaris_query: str | None = None,
        row_count: int | None = None,
        confidence: float | None = None,
        retry_count: int = 0,
        status: str = "success",
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        record: dict[str, Any] = {
            "timestamp": utc_now_iso(),
            "run_id": self.run_id,
            "trace_id": self.trace_id,
            "agent_name": agent_name,
            "action": action,
            "input_summary": _truncate(input_summary),
            "output_summary": _truncate(output_summary),
            "target_agent": target_agent,
            "model": model,
            "engine": engine,
            "zetaris_query": _truncate(zetaris_query, 800) if zetaris_query else None,
            "row_count": row_count,
            "confidence": confidence,
            "retry_count": retry_count,
            "status": status,
        }
        if extra:
            record.update(extra)

        line = redact(json.dumps(record, default=str), self._secrets)

        with self._lock:
            if model and model not in self.models_used:
                self.models_used.append(model)
            if engine and engine not in self.engines_used:
                self.engines_used.append(engine)
            if zetaris_query:
                self.zetaris_queries += 1
            self.events.append(json.loads(line))
            with self.log_file.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            if self._echo:
                self._print(record)
        return record

    def _print(self, record: dict[str, Any]) -> None:
        bits = [f"[{record['agent_name']}]", record["action"]]
        if record.get("model"):
            bits.append(f"model={record['model']}")
        if record.get("engine"):
            bits.append(f"engine={record['engine']}")
        if record.get("row_count") is not None:
            bits.append(f"rows={record['row_count']}")
        if record.get("confidence") is not None:
            bits.append(f"conf={record['confidence']:.2f}")
        if record["status"] != "success":
            bits.append(f"status={record['status']}")
        summary = record.get("output_summary") or record.get("input_summary") or ""
        line = " ".join(str(b) for b in bits)
        if summary:
            line += f" :: {summary}"
        print(redact(line, self._secrets), file=sys.stderr)

    # ------------------------------------------------------------ shortcuts

    def handoff(self, frm: str, to: str, note: str = "") -> None:
        self.log(agent_name=frm, action="handoff", target_agent=to, output_summary=note)

    def engine_op(self, engine: str, action: str, summary: str = "", agent_name: str = "Orchestrator") -> None:
        self.log(agent_name=agent_name, action=action, engine=engine, output_summary=summary)

    def counts(self) -> dict[str, Any]:
        return {
            "models_used": list(self.models_used),
            "engines_used": list(self.engines_used),
            "zetaris_queries": self.zetaris_queries,
            "events": len(self.events),
        }
