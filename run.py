#!/usr/bin/env python3
"""Pulsar 5 entry point.

    python run.py                                      # start the API on $PORT
    python run.py --input input_examples/example_1.json
    python run.py --input input_examples/example_1.json --event input_examples/event_track_shift.json
    python run.py --check                               # connectivity only
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.adapters.nightshift import NightshiftClient
from app.adapters.zetaris import ZetarisClient
from app.config import load_settings
from app.orchestrator import Orchestrator
from app.schemas import EventRequest, RunRequest


def cmd_check() -> int:
    """Step 1 of the setup guide: verify connectivity, print no secrets."""
    settings = load_settings()
    zetaris = ZetarisClient(settings)
    nightshift = NightshiftClient(settings)
    try:
        z = zetaris.health()
        n = nightshift.health()
        print("Zetaris   :", json.dumps(z))
        print("Nightshift:", json.dumps(n))
        routes = n.get("nemotron_routes") or []
        if routes:
            print("\nNemotron-backed routes:")
            for route in routes:
                print(f"  {route}")
        else:
            print("\nNo Nemotron-backed routes visible.")
        print("\nLanes in use:")
        for lane in ("fast", "deep", "judge"):
            print(f"  {lane:<6} -> {settings.model_for_lane(lane)}")
        return 0 if (z.get("reachable") and n.get("reachable")) else 1
    finally:
        zetaris.close()
        nightshift.close()


def cmd_run(input_path: Path, event_path: Path | None, out_path: Path | None) -> int:
    request = RunRequest(**json.loads(input_path.read_text(encoding="utf-8")))
    orchestrator = Orchestrator()
    response = orchestrator.run(request)

    payload: dict = {"run": response.model_dump(mode="json")}

    if event_path and response.status != "failed":
        raw = json.loads(event_path.read_text(encoding="utf-8"))
        raw["run_id"] = request.run_id  # the event always targets the run we just made
        event_request = EventRequest(**raw)
        event_response = orchestrator.apply_event(event_request.run_id, event_request.event)
        payload["event"] = event_response.model_dump(mode="json")

    text = json.dumps(payload, indent=2)
    if out_path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text + "\n", encoding="utf-8")
        print(f"\nwrote {out_path}", file=sys.stderr)
    else:
        print(text)

    return 0 if response.status in {"completed", "escalated"} else 1


def cmd_serve() -> int:
    import uvicorn

    settings = load_settings()
    uvicorn.run("app.main:app", host="0.0.0.0", port=settings.port, log_level=settings.log_level.lower())
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Pulsar 5")
    parser.add_argument("--input", type=Path, help="run one input file instead of serving")
    parser.add_argument("--event", type=Path, help="after the run, inject this event file")
    parser.add_argument("--out", type=Path, help="write the result here instead of stdout")
    parser.add_argument("--check", action="store_true", help="verify Zetaris and Nightshift connectivity")
    args = parser.parse_args()

    if args.check:
        return cmd_check()
    if args.input:
        return cmd_run(args.input, args.event, args.out)
    return cmd_serve()


if __name__ == "__main__":
    raise SystemExit(main())
