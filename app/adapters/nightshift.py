"""Meterless Nightshift adapter — the only model provider in Pulsar 5.

There is no `openai`, `anthropic` or NVIDIA client anywhere in this codebase on
purpose: every model call is an OpenAI-compatible POST to the Nightshift
Runtime URL, and the model name behind each call is recorded in the trace (the
DGX Spark award needs that evidence). The NVIDIA API key lives only inside the
Nightshift app and is never read by this process.

Lanes map to pinned Nemotron routes:
    fast  -> Analyst tool calls, extraction, Risk classification
    deep  -> Planner, Belief Reviser
    judge -> Critic

When Nightshift is unreachable, or SAMPLE_MODE is on with no recorded
response, `chat` returns a result with `fallback_used=True` and no text. Each
caller then runs its own deterministic path, so the agent logic still executes
with no network. Callers never fake a model answer.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import httpx

from app.config import SAMPLE_CACHE_DIR, Settings
from app.tracing import Tracer

LANES = ("fast", "deep", "judge")

# Nemotron reasoning models (lightning-30b among them) emit `reasoning_content`
# BEFORE `content`, and both draw on max_tokens. Ask for 200 and the reasoning
# consumes the whole budget: the call returns HTTP 200 with content=None and
# finish_reason="length". That looks exactly like a failure to a caller, so a
# working provider would silently degrade every agent to its fallback path.
# This floor keeps enough budget for an answer after the thinking.
MIN_MAX_TOKENS = 1200


@dataclass
class ChatResult:
    text: str | None
    model: str
    lane: str
    fallback_used: bool = False
    reason: str | None = None
    elapsed_seconds: float = 0.0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return bool(self.text) and not self.fallback_used

    def json(self) -> Any | None:
        """Parse the reply as JSON, tolerating ```json fences and prose around it."""
        if not self.text:
            return None
        return extract_json(self.text)


def extract_json(text: str) -> Any | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def prompt_fingerprint(model: str, messages: Iterable[dict[str, str]]) -> str:
    """Stable key for a recorded response: the lane's model plus the messages."""
    payload = json.dumps(
        {"model": model, "messages": [{"role": m.get("role"), "content": m.get("content")} for m in messages]},
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:20]


class NightshiftClient:
    def __init__(
        self,
        settings: Settings,
        tracer: Tracer | None = None,
        *,
        cache_dir: Path | None = None,
    ) -> None:
        self.settings = settings
        self.tracer = tracer
        self.cache_dir = cache_dir or (SAMPLE_CACHE_DIR / "llm")
        self._client: httpx.Client | None = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.settings.llm_timeout_seconds)
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.settings.meterless_token}",
            "Content-Type": "application/json",
            "meterless-app": "pulsar5",
            "meterless-scope": "user",
        }

    # ----------------------------------------------------------------- public

    def chat(
        self,
        lane: str,
        messages: list[dict[str, str]],
        *,
        agent_name: str,
        action: str = "llm_call",
        temperature: float = 0.1,
        max_tokens: int = 1200,
        expect_json: bool = True,
    ) -> ChatResult:
        if lane not in LANES:
            raise ValueError(f"lane must be one of {LANES}, got {lane!r}")

        model = self.settings.model_for_lane(lane)
        max_tokens = max(max_tokens, MIN_MAX_TOKENS)
        started = time.monotonic()

        if self.settings.sample_mode:
            recorded = self._recorded(model, messages)
            elapsed = time.monotonic() - started
            if recorded is not None:
                result = ChatResult(recorded, model, lane, elapsed_seconds=elapsed, reason="recorded_response")
            else:
                result = ChatResult(
                    None, model, lane, fallback_used=True, reason="no_recorded_response", elapsed_seconds=elapsed
                )
            self._trace(agent_name, action, lane, result, status="sample_mode")
            return result

        if not self.settings.nightshift_configured:
            result = ChatResult(
                None, model, lane, fallback_used=True, reason="nightshift_not_configured",
                elapsed_seconds=time.monotonic() - started,
            )
            self._trace(agent_name, action, lane, result, status="fallback")
            return result

        body: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if expect_json:
            body["response_format"] = {"type": "json_object"}

        last_reason = "unknown_error"
        for attempt in range(self.settings.llm_max_retries + 1):
            try:
                response = self._http().post(
                    f"{self.settings.meterless_base_url}/chat/completions",
                    headers=self._headers(),
                    json=body,
                )
                if response.status_code >= 400:
                    # 502 here usually means Nightshift reached the provider and the
                    # provider refused the key. Worth distinguishing in the trace.
                    last_reason = f"http_{response.status_code}"
                    raise httpx.HTTPStatusError(last_reason, request=response.request, response=response)
                payload = response.json()
                text = _first_message(payload)
                served = payload.get("model") or model

                if not text:
                    # 200 with no content. Almost always a reasoning model that
                    # spent the budget thinking. Say so, rather than reporting a
                    # generic failure that sends someone hunting the wrong bug.
                    choice = (payload.get("choices") or [{}])[0]
                    reason = choice.get("finish_reason")
                    thought = bool((choice.get("message") or {}).get("reasoning_content"))
                    last_reason = (
                        f"empty_content(finish_reason={reason}"
                        + (", reasoning_content present" if thought else "")
                        + f", max_tokens={max_tokens})"
                    )
                    raise ValueError(last_reason)
                result = ChatResult(
                    text, served, lane, elapsed_seconds=time.monotonic() - started, raw=payload
                )
                self._trace(agent_name, action, lane, result, retry_count=attempt)
                return result
            except Exception as exc:
                last_reason = f"{type(exc).__name__}: {exc}"
                if self.tracer:
                    self.tracer.log(
                        agent_name=agent_name,
                        action=f"{action}_retry",
                        model=model,
                        output_summary=last_reason,
                        retry_count=attempt,
                        status="retry" if attempt < self.settings.llm_max_retries else "error",
                        extra={"lane": lane},
                    )
                if attempt < self.settings.llm_max_retries:
                    time.sleep(0.75 * (2**attempt))

        result = ChatResult(
            None, model, lane, fallback_used=True, reason=last_reason,
            elapsed_seconds=time.monotonic() - started,
        )
        self._trace(agent_name, action, lane, result, status="fallback")
        return result

    def list_models(self) -> list[dict[str, Any]]:
        if self.settings.sample_mode or not self.settings.nightshift_configured:
            return []
        response = self._http().get(f"{self.settings.meterless_base_url}/models", headers=self._headers())
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else payload
        return [m for m in (data or []) if isinstance(m, dict)]

    def nemotron_routes(self) -> list[str]:
        return [str(m.get("id")) for m in self.list_models() if "nemotron" in str(m.get("id", "")).lower()]

    def health(self) -> dict[str, Any]:
        if self.settings.sample_mode:
            return {"configured": self.settings.nightshift_configured, "reachable": False, "mode": "sample"}
        if not self.settings.nightshift_configured:
            return {"configured": False, "reachable": False, "mode": "live"}
        try:
            models = self.list_models()
            return {
                "configured": True,
                "reachable": True,
                "mode": "live",
                "model_count": len(models),
                "nemotron_routes": [m for m in (str(x.get("id")) for x in models) if "nemotron" in m.lower()],
            }
        except Exception as exc:
            return {"configured": True, "reachable": False, "mode": "live", "error": type(exc).__name__}

    # ---------------------------------------------------------------- private

    def _recorded(self, model: str, messages: list[dict[str, str]]) -> str | None:
        path = self.cache_dir / f"{prompt_fingerprint(model, messages)}.json"
        if not path.exists():
            return None
        blob = json.loads(path.read_text(encoding="utf-8"))
        text = blob.get("response")
        return text if isinstance(text, str) else json.dumps(text)

    def record(self, model: str, messages: list[dict[str, str]], response: str) -> Path:
        """Used by scripts/record_samples.py to build the SAMPLE_MODE cache."""
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        path = self.cache_dir / f"{prompt_fingerprint(model, messages)}.json"
        path.write_text(
            json.dumps({"model": model, "messages": messages, "response": response}, indent=2),
            encoding="utf-8",
        )
        return path

    def _trace(
        self,
        agent_name: str,
        action: str,
        lane: str,
        result: ChatResult,
        *,
        retry_count: int = 0,
        status: str = "success",
    ) -> None:
        if not self.tracer:
            return
        if result.fallback_used:
            summary = f"no model answer ({result.reason}); agent uses deterministic path"
            status = "fallback"
        else:
            summary = (result.text or "")[:300]
        self.tracer.log(
            agent_name=agent_name,
            action=action,
            model=result.model,
            output_summary=summary,
            retry_count=retry_count,
            status=status,
            extra={
                "lane": lane,
                "fallback_used": result.fallback_used,
                "latency_seconds": round(result.elapsed_seconds, 3),
            },
        )


def _first_message(payload: dict[str, Any]) -> str | None:
    choices = payload.get("choices") or []
    if not choices:
        return None
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # some runtimes return content parts
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    # A reasoning model's `reasoning_content` is deliberately NOT used as the
    # answer: it is the model thinking aloud, not its reply, and feeding it to a
    # JSON parser downstream would be worse than falling back cleanly.
    return None
