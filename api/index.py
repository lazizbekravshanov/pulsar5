"""Vercel serverless entry point.

Vercel's Python runtime serves an ASGI app exported as `app` from a file under
`api/`. This is a thin adapter, not a second application: it points at the same
`app.main:app` the local server and the Docker image run, so what a judge opens
in a browser is the same code path as `python run.py`.

The hosted demo runs in SAMPLE_MODE because there is no Zetaris instance and no
Nightshift runtime in the cloud — both are local services. SAMPLE_MODE still
executes the real agent loop (Analyst, Planner, Simulator, Critic, Risk, Belief
Reviser) against recorded Zetaris results, so the plan, the critique, the
escalations and the change-of-mind diff are all genuinely computed per request.
Nothing here is a canned response.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# The function bundle puts the repo at the parent of api/; make the app package
# importable before anything reads from it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Vercel mounts the deployment read-only apart from /tmp, so the tracer and the
# H-MEM store have to live there. app/config.py falls back on its own, but
# setting them explicitly keeps the behaviour obvious to anyone reading this.
os.environ.setdefault("PULSAR5_LOGS_DIR", "/tmp/pulsar5-logs")
os.environ.setdefault("PULSAR5_HMEM_DIR", "/tmp/pulsar5-hmem")

# No Zetaris or Nightshift reachable from a Vercel function.
os.environ.setdefault("SAMPLE_MODE", "true")

from app.main import app  # noqa: E402  (import after sys.path and env are set)

__all__ = ["app"]
