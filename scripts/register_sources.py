#!/usr/bin/env python3
"""Register every Pulsar 5 data source in Zetaris, one statement at a time.

    python scripts/register_sources.py                 # dry run, prints the plan
    python scripts/register_sources.py --apply         # actually create things
    python scripts/register_sources.py --apply --only 01_storm_advisories
    python scripts/register_sources.py --verify        # query each view

Dry run is the default on purpose: the setup guide says to dry-run before
creating anything in Zetaris. Nothing here prints an API key.

`${RAW_BASE}` in the SQL is replaced with the raw GitHub base for this
repository, taken from --raw-base, then PULSAR5_RAW_BASE, then the git remote.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.adapters.zetaris import ZetarisClient  # noqa: E402
from app.config import load_settings  # noqa: E402

SQL_DIR = ROOT / "scripts" / "zetaris"
VIEWS = [
    "storm_advisories",
    "shelters",
    "hospitals",
    "acs_vulnerability",
    "disaster_declarations",
    "staging_sites",
    "road_events",
    "storm_warnings_archive",
]


def raw_base(explicit: str | None) -> str:
    if explicit:
        return explicit.rstrip("/")

    import os

    from_env = os.getenv("PULSAR5_RAW_BASE")
    if from_env:
        return from_env.rstrip("/")

    try:
        remote = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "https://raw.githubusercontent.com/OWNER/REPO/main"

    match = re.search(r"github\.com[:/](?P<owner>[^/]+)/(?P<repo>[^/.]+)", remote)
    if not match:
        return "https://raw.githubusercontent.com/OWNER/REPO/main"
    return f"https://raw.githubusercontent.com/{match['owner']}/{match['repo']}/main"


def _already_exists(message: str) -> bool:
    """True when the instance is telling us the object is already registered.

    Lightning SQL has no IF NOT EXISTS for CREATE SCHEMASTORE CONTAINER, so a
    second run of a registration file fails on an object the first run created.
    That is not a real failure, and re-running registration has to be safe.
    """
    lowered = message.lower()
    return "already exists" in lowered or "already registered" in lowered


def split_statements(sql: str) -> list[str]:
    """One statement per element, comments stripped.

    Zetaris takes a single statement per request, so this never hands it a
    batch. Splitting on `;` is safe here because none of our DDL contains a
    semicolon inside a string literal.
    """
    without_comments = "\n".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    )
    return [stmt.strip() for stmt in without_comments.split(";") if stmt.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Register Pulsar 5 sources in Zetaris")
    parser.add_argument("--apply", action="store_true", help="run the statements (default is a dry run)")
    parser.add_argument("--verify", action="store_true", help="query each registered view")
    parser.add_argument("--only", help="run just one SQL file, by stem")
    parser.add_argument("--raw-base", help="raw GitHub base URL for this repository")
    parser.add_argument("--continue-on-error", action="store_true", help="keep going past a failure")
    parser.add_argument(
        "--skip-exists",
        action="store_true",
        help="treat 'already exists' as success (CREATE SCHEMASTORE CONTAINER has no IF NOT EXISTS)",
    )
    args = parser.parse_args()

    settings = load_settings()
    settings.sample_mode = False  # registration is meaningless against a cache

    base = raw_base(args.raw_base)
    files = sorted(SQL_DIR.glob("*.sql"))
    if args.only:
        files = [f for f in files if f.stem == args.only or f.stem.startswith(args.only)]
        if not files:
            print(f"no SQL file matching {args.only!r}", file=sys.stderr)
            return 2

    print(f"raw base : {base}")
    print(f"zetaris  : {settings.zetaris_rest_url or '(not configured)'}")
    print(f"mode     : {'APPLY' if args.apply else 'DRY RUN'}\n")

    if args.apply and not settings.zetaris_configured:
        print("ZETARIS_REST_URL and ZETARIS_API_KEY must be set to apply.", file=sys.stderr)
        return 2

    client = ZetarisClient(settings, agent_name="Registrar")
    failures: list[tuple[str, str]] = []
    total = 0

    try:
        for path in files:
            statements = split_statements(path.read_text(encoding="utf-8").replace("${RAW_BASE}", base))
            print(f"=== {path.name} ({len(statements)} statements)")
            for index, statement in enumerate(statements, start=1):
                headline = " ".join(statement.split())[:96]
                total += 1
                if not args.apply:
                    print(f"  [{index}] would run: {headline}")
                    continue
                try:
                    result = client.run_sql(statement, query_name=f"{path.stem}#{index}")
                    print(f"  [{index}] ok ({result.row_count} rows): {headline}")
                except Exception as exc:
                    message = str(exc)
                    if args.skip_exists and _already_exists(message):
                        print(f"  [{index}] exists, skipped: {headline}")
                        continue
                    print(f"  [{index}] FAILED: {headline}\n        {type(exc).__name__}: {exc}")
                    failures.append((f"{path.name}#{index}", f"{type(exc).__name__}: {exc}"))
                    if not args.continue_on_error:
                        hint = (
                            "\nStopping. Re-run with --continue-on-error to push past this"
                            + (", or --skip-exists if these objects are already registered." if _already_exists(message) else ".")
                        )
                        print(hint)
                        return 1
            print()

        if args.verify:
            print("=== verifying views")
            for view in VIEWS:
                try:
                    result = client.run_sql(f"SELECT * FROM pulsar5.{view} LIMIT 5", query_name=f"verify_{view}")
                    status = "ok" if result.row_count else "EMPTY"
                    print(f"  {view:<26} {status} ({result.row_count} rows)")
                    if not result.row_count:
                        failures.append((view, "returned no rows"))
                except Exception as exc:
                    print(f"  {view:<26} FAILED: {type(exc).__name__}: {exc}")
                    failures.append((view, f"{type(exc).__name__}: {exc}"))
    finally:
        client.close()

    print(f"\n{total} statements {'run' if args.apply else 'planned'}, {len(failures)} problem(s)")
    for name, reason in failures:
        print(f"  - {name}: {reason}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
