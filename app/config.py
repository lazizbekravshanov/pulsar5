"""Runtime settings, loaded once from the environment.

Secrets are read here and never logged. `tracing.redact` is the only place
allowed to turn a secret-bearing string into something printable.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

try:  # optional: .env is a developer convenience, not a requirement
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover
    pass

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
SAMPLE_CACHE_DIR = DATA_DIR / "sample_cache"
REPLAY_DIR = DATA_DIR / "replay"


def _writable_dir(env_var: str, preferred: Path, fallback_name: str) -> Path:
    """A directory we can actually write to at runtime.

    Serverless hosts mount the deployment read-only and give you /tmp. Writing a
    trace or an H-MEM store under the repo works locally and 500s there, so the
    path is settable by env and falls back to /tmp when the preferred one cannot
    be created or written. Read-only paths (the sample cache, the replay data)
    stay in the repo, because they ship with it.
    """
    override = os.getenv(env_var)
    if override:
        return Path(override)
    try:
        preferred.mkdir(parents=True, exist_ok=True)
        probe = preferred / ".write-probe"
        probe.touch()
        probe.unlink()
        return preferred
    except OSError:
        return Path(tempfile.gettempdir()) / fallback_name


LOGS_DIR = _writable_dir("PULSAR5_LOGS_DIR", REPO_ROOT / "logs", "pulsar5-logs")
HMEM_DIR = _writable_dir("PULSAR5_HMEM_DIR", DATA_DIR / "hmem", "pulsar5-hmem")


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    try:
        return int(raw) if raw not in (None, "") else default
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    try:
        return float(raw) if raw not in (None, "") else default
    except ValueError:
        return default


@dataclass
class Settings:
    # Zetaris
    zetaris_rest_url: str = field(default_factory=lambda: os.getenv("ZETARIS_REST_URL", "").rstrip("/"))
    zetaris_org_id: str = field(default_factory=lambda: os.getenv("ZETARIS_ORG_ID", "1"))
    zetaris_api_key: str = field(default_factory=lambda: os.getenv("ZETARIS_API_KEY", ""))

    # Nightshift
    meterless_base_url: str = field(default_factory=lambda: os.getenv("METERLESS_BASE_URL", "").rstrip("/"))
    meterless_token: str = field(default_factory=lambda: os.getenv("METERLESS_RUNTIME_TOKEN", ""))
    model_default: str = field(default_factory=lambda: os.getenv("METERLESS_MODEL", "meterless/auto"))
    model_fast: str = field(default_factory=lambda: os.getenv("METERLESS_MODEL_FAST", ""))
    model_deep: str = field(default_factory=lambda: os.getenv("METERLESS_MODEL_DEEP", ""))
    model_judge: str = field(default_factory=lambda: os.getenv("METERLESS_MODEL_JUDGE", ""))

    # Runtime
    sample_mode: bool = field(default_factory=lambda: _bool("SAMPLE_MODE", False))
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO").upper())
    port: int = field(default_factory=lambda: _int("PORT", 8000))
    max_replan_iterations: int = field(default_factory=lambda: _int("MAX_REPLAN_ITERATIONS", 3))
    llm_timeout_seconds: float = field(default_factory=lambda: _float("LLM_TIMEOUT_SECONDS", 60.0))
    zetaris_timeout_seconds: float = field(default_factory=lambda: _float("ZETARIS_TIMEOUT_SECONDS", 45.0))
    confidence_escalation_threshold: float = field(
        default_factory=lambda: _float("CONFIDENCE_ESCALATION_THRESHOLD", 0.7)
    )
    llm_max_retries: int = field(default_factory=lambda: _int("LLM_MAX_RETRIES", 2))

    def model_for_lane(self, lane: str) -> str:
        """Lane -> pinned Nemotron route, falling back to the default route."""
        pinned = {"fast": self.model_fast, "deep": self.model_deep, "judge": self.model_judge}.get(lane, "")
        return pinned or self.model_default

    @property
    def zetaris_configured(self) -> bool:
        return bool(self.zetaris_rest_url and self.zetaris_api_key)

    @property
    def nightshift_configured(self) -> bool:
        return bool(self.meterless_base_url and self.meterless_token)

    def secrets(self) -> list[str]:
        """Every value that must never reach a log, a trace or stdout."""
        return [v for v in (self.zetaris_api_key, self.meterless_token) if v]


def load_settings(**overrides: object) -> Settings:
    settings = Settings()
    for key, value in overrides.items():
        if value is not None and hasattr(settings, key):
            setattr(settings, key, value)
    return settings
