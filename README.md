# Pulsar 5

**A disaster response planning agent that changes its mind correctly.**

Open Agent Hackathon 2026 · Track: Reasoning Architecture

Pulsar 5 helps a county emergency manager decide **which shelters to open, where to stage supplies, and which zones to evacuate first** in the hours before a hurricane. It fuses weather advisories, shelter capacity, hospitals and census vulnerability through Zetaris, proposes a plan, **simulates it before committing**, has a Critic reject it against the cited evidence, and escalates every irreversible call to a human.

The part we care about most: when the world changes mid-run, it **replans only what actually depended on what changed**.

---

## The demo: Hurricane Ian, 26 September 2022

At advisory 15, the official forecast pointed at **Tampa Bay**. Landfall came 100 miles south, at **Fort Myers in Lee County**.

Run Pulsar 5 at that hour and it plans for Tampa, because that is what the evidence said. Then inject advisory 23:

```
BEFORE  advisory 15 (Tampa cone)      Charlotte 2 · Hillsborough 3 · Pinellas 3 · Sarasota 3
AFTER   advisory 23 (Fort Myers cone) Charlotte 3 · Lee 3 · Pinellas 1 · Sarasota 2

27 beliefs changed · 11 decisions invalidated · 36 preserved · plan diff +4 / −6 / ~31
```

Lee County enters with all three zones. Hillsborough stands down. **Sarasota is preserved** — it was in both cones, so the decisions that cited it were never invalidated. That is the difference between a targeted replan and starting over.

And at advisory 15, hours before any forecast pointed there, Pulsar 5 had already put this in front of the operator:

> `distant_vulnerable_zone`: Zone FL-071-0500 (Lee) scores 0.83 on social vulnerability but lies 48 mi outside the current cone. No evacuation is warranted yet; pre-positioning there is an operator call.

---

## Try it without installing anything

**https://pulsar5-nine.vercel.app**

Pick an hour, press Plan, then press *Advisory 23 moves the track south* and watch it change its mind.

The hosted demo runs in `SAMPLE_MODE`, because Zetaris and Nightshift are local services with no cloud instance to reach. That is not a shortcut: SAMPLE_MODE executes the whole agent loop against recorded Zetaris results, so the plan, the Critic's rejection, the escalations and the change-of-mind diff are all computed per request. The trace says plainly which model calls fell back. For the sponsor integrations running live against a real Zetaris instance and real Nemotron routes, see [`logs/trace-sample.jsonl`](logs/trace-sample.jsonl), captured from a local run.

## Run it in 60 seconds

No Zetaris, no Nightshift, no credentials, no network:

```bash
git clone https://github.com/lazizbekravshanov/pulsar5.git && cd pulsar5
python3.11 -m venv .venv && ./.venv/bin/pip install -r requirements.txt
SAMPLE_MODE=true ./.venv/bin/python run.py --input input_examples/example_1.json
```

Or over HTTP, the way a judge should:

```bash
SAMPLE_MODE=true ./.venv/bin/python run.py          # serves on :8000, UI at /
curl -X POST localhost:8000/run -H 'Content-Type: application/json' -d @input_examples/example_1.json
curl -X POST localhost:8000/events -H 'Content-Type: application/json' -d @input_examples/event_track_shift.json
```

Against a real Zetaris instance, `--live` overrides the example's pinned `sample_mode`:

```bash
./.venv/bin/python run.py --check                                  # both services
./.venv/bin/python run.py --input input_examples/example_1.json --live
```

Live and SAMPLE_MODE produce the same 29-action plan. That is the point: the cache is faithful, not a demo branch. Live takes about 25 s against real HTTP; the cache takes 5 ms.

Or in Docker:

```bash
docker build -t pulsar5 . && docker run --rm -p 8000:8000 -e SAMPLE_MODE=true pulsar5
```

`SAMPLE_MODE` runs **the real agent logic** against recorded Zetaris results. It is not a demo branch: the same Analyst, Planner, Simulator, Critic, Risk and Belief Reviser execute, the same engines run, the same loop iterates. See [SAMPLE_MODE](#sample_mode-what-is-real-and-what-is-recorded).

---

## Architecture: the LLM proposes, the simulator and critic verify

```
          ┌──────────────── Zetaris: one SQL access point ─────────────────┐
          │ storm_advisories · shelters · hospitals · acs_vulnerability ·  │
          │ staging_sites · road_events · disaster_declarations            │
          └──────────────────────────┬────────────────────────────────────┘
                                     │ POST /sql-editor/sqls/run
   event ──►┌────────────────────────▼─────────────────────────┐
            │  WORLD MODEL (Meterless)                          │◄── shared state:
            │  beliefs + provenance · decision dependency graph │    every agent reads
            └──┬──────────┬──────────┬──────────┬───────────────┘    and writes here
               │          │          │          │
     ┌─────────▼──┐  ┌────▼────┐ ┌───▼─────┐ ┌──▼─────┐   ┌──────────────┐
     │ Belief     │─►│ Planner │⇄│Simulator│⇄│ Critic │──►│ Risk /       │
     │ Reviser    │  │ (deep)  │ │ (code)  │ │(judge) │   │ Escalation   │
     │ diff+impact│  └────┬────┘ └─────────┘ └────────┘   │ human gate   │
     └────────────┘       │ ▲                              └──────────────┘
                     ┌────▼─┴────┐
                     │  Analyst  │ tool-calling over Zetaris (fast)
                     └───────────┘

   H-MEM: lessons across runs      Markovian: hourly replay in bounded steps
```

The loop is not a chain. The Critic can **reject** and send refinement instructions back to the Planner, bounded by `MAX_REPLAN_ITERATIONS` (default 3). A typical run takes 2 iterations: the first budgeted plan falls short on capacity, the Critic refuses it, the revision opens more shelters, the Critic accepts at 0.90.

### The six agents

| Agent | Lane | What it actually does | File |
|---|---|---|---|
| **Analyst** | fast | Tool-calling loop: the Fast lane picks queries from a fixed menu, runs them through Zetaris, and writes every row into the World Model with its source table, exact SQL and `as_of` timestamp | [`app/agents/analyst.py`](app/agents/analyst.py) |
| **Planner** | deep | Authors the plan. The only agent allowed to create actions. Every action cites World Model fact ids | [`app/agents/planner.py`](app/agents/planner.py) |
| **Simulator** | code + fast | Deterministic capacity, coverage, distance and budget checks. Returns structured `Violation` objects, never prose. The verdict never depends on a model | [`app/agents/simulator.py`](app/agents/simulator.py), [`app/sim/checks.py`](app/sim/checks.py) |
| **Critic** | judge | Four mechanical checks (evidence exists, evidence is fresh enough for its source, no high-severity violation, no vulnerable zone missed) plus a Judge-lane review. Can reject | [`app/agents/critic.py`](app/agents/critic.py) |
| **Risk** | fast | Classifies reversible vs irreversible. An evacuation order **never** auto-commits. The Fast lane may only escalate *more*, never less | [`app/agents/risk.py`](app/agents/risk.py) |
| **Belief Reviser** | deep | On an event: diffs the World Model, walks the decision dependency graph, invalidates exactly the dependents, triggers a targeted replan | [`app/agents/belief_reviser.py`](app/agents/belief_reviser.py) |

Three design decisions worth pointing at, because they are where the reasoning lives:

**1. Not every finding is the Planner's to fix.** A plan that is short on shelter space because the Planner was too conservative goes back for revision. A plan that is short because *the region does not have that many shelter spaces* is a `regional_capacity_shortfall` — no planning move invents capacity, so it escalates instead of burning three iterations. Same for stale data (the Analyst's job, triggered automatically on an advisory change) and for pre-positioning outside the cone (the operator's call).

**2. Freshness is a property of the source, not the clock.** A shelter census 18 hours behind the advisory is stale. ACS 5-year demographic estimates are years old *by construction* and flagging them against a storm advisory would reject every plan forever. See `TIMELESS_SOURCES` in [`app/agents/critic.py`](app/agents/critic.py).

**3. Confidence counts distinct problems, not repetitions.** One stale feed cited by twenty actions is one problem seen twenty times. Counting instances drove confidence to the floor and escalated sound plans, which teaches an operator to ignore the number.

---

## The three Meterless engines, each doing real work

| Engine | Job in Pulsar 5 | Proof | File |
|---|---|---|---|
| **World Model** | The shared state every agent reads and writes: beliefs with provenance, decisions with dependencies. Concurrent writes merge per key; a late write with an older `as_of` is recorded as a conflict rather than clobbering fresher data | 8 threads × 60 writes, zero lost ([`tests/test_world_model.py`](tests/test_world_model.py)) | [`app/engines/world_model/engine.py`](app/engines/world_model/engine.py) |
| **H-MEM** | Three-level memory across runs (L0 episodes → L1 lessons → L2 standing policy). A recalled lesson writes a `memory_derate` onto a shelter, so run 2 plans against smaller usable capacity than run 1. An operator's approve or decline on a held action is remembered the same way and recalled on the next run for the same counties | A fresh process recalls run 1's lesson and the capacity numbers change ([`tests/test_hmem.py`](tests/test_hmem.py), [`tests/test_integration.py`](tests/test_integration.py)) | [`app/engines/hmem/engine.py`](app/engines/hmem/engine.py) |
| **Markovian** | The hourly replay. Each hour is one bounded step with a compressed carryover, and H-MEM is injected once at step 1 per the COMBINE contract | **24 steps, context flat at 448–452 chars** while each step returns 20 KB ([`tests/test_markovian.py`](tests/test_markovian.py)) | [`app/engines/markovian/engine.py`](app/engines/markovian/engine.py) |

The H-MEM derate is deliberately wired into `app/sim/capacity.py`, shared by Planner **and** Simulator. If they disagreed, a lesson would be decoration: the Planner would treat a shelter as smaller while the Simulator kept checking against nominal headroom.

---

## Sponsor technology

**Zetaris is the data layer.** Every fact Pulsar 5 reasons about arrives through `POST /sql-editor/sqls/run`. Nothing in `app/` opens a data file or calls NWS, FEMA or Census directly. Registration is scripted, not manual — [`scripts/zetaris/*.sql`](scripts/zetaris/) and [`scripts/register_sources.py`](scripts/register_sources.py), which dry-runs by default and sends **one statement at a time**. The REST table pattern is copied from the Zetaris getting-started PokéAPI recipe.

**Meterless Nightshift is the only model provider.** There is no `openai`, `anthropic` or NVIDIA client anywhere in this repository — grep for it. Every model call is an OpenAI-compatible POST to the Nightshift Runtime URL with `meterless-app: pulsar5`, and **the model that served each call is recorded in the trace**:

```json
{"agent_name":"Critic","action":"reject_plan","model":"nvidia/nemotron-3.5-bolt-e4b",
 "lane":"judge","confidence":0.56,"output_summary":"1 fixable findings ..."}
```

**NVIDIA Nemotron through Nightshift, three lanes:** fast (Analyst, Simulator narration, Risk), deep (Planner, Belief Reviser), judge (Critic). Pin them with `METERLESS_MODEL_FAST/DEEP/JUDGE`; `python run.py --check` lists which catalogue routes are Nemotron-backed. **The NVIDIA API key lives only inside the Nightshift app** and is never read by this process.

Setup for both: the [Participant Setup Guide](https://zetaris.github.io/getting-started/) for Zetaris, and [meterless.ai](https://meterless.ai) for Nightshift.

---

## Data sources

| Zetaris table | Source | Licence | Notes |
|---|---|---|---|
| `storm_advisories` | NHC public advisory archive for Ian, served from this repo by raw URL | US Government work, public domain | Track shape and the Tampa→south shift follow the public advisories; positions rounded to 0.1°, cones simplified. Regenerate: `python scripts/build_replay.py` |
| `shelters` | FEMA ESF#6 National Shelter System (ArcGIS REST) | FEMA open data | Field names vary between NSS layers — verify against a live payload before the demo |
| `hospitals` | HIFLD Open via the NASA NCCS mirror | Public domain | HIFLD Open was deactivated 26 Aug 2025 |
| `acs_vulnerability` | Census ACS 5-year, tract level | US Census, public domain | Drives evacuation priority. Treated as a timeless baseline |
| `disaster_declarations` | OpenFEMA DisasterDeclarationsSummaries | FEMA open data | Ian is DR-4673 |
| `storm_warnings_archive` | Iowa Environmental Mesonet NWS warning archive | Freely redistributable | Used instead of `api.weather.gov/alerts`, which keeps ~7 days and needs a `User-Agent` header a Lightning REST table cannot send. We do not bypass Zetaris to get it |
| `staging_sites`, `road_events` | **SYNTHETIC**, generated by `scripts/build_replay.py` | — | Labelled synthetic in the data files and in the registration SQL |

---

## SAMPLE_MODE: what is real and what is recorded

| | Live mode | SAMPLE_MODE |
|---|---|---|
| Agent logic, loop, engines, escalation | real | **real, identical** |
| Data | Zetaris REST | recorded results in `data/sample_cache/sql/` |
| Model calls | Nightshift | recorded responses, else each agent's deterministic path |

The cache holds a **superset** and each named query declares the predicates its SQL applies (`where_in`, `where_lte`, `order_desc`, `limit`), so SAMPLE_MODE narrows the recorded rows exactly as Zetaris would. A two-county request really does plan two counties; replay hour 14 really does see advisory 14. Without that the cache would hand every request the whole table and SAMPLE_MODE would quietly stop matching live mode.

When no recorded model response exists, each agent runs its own deterministic path and the trace says so explicitly (`"status":"fallback"`). **Agents never fabricate a model answer.** SAMPLE_MODE never reads `output_examples/` — there is a test that enforces it.

---

## Reliability scoreboard

```bash
./.venv/bin/python scripts/eval.py
```

35 perturbed scenarios — the advisory track hour by hour, region subsets, starved and plentiful supply, missing and contradictory sources, 4/12/24-hour replays, a 1-iteration cap:

```
constraint_violations_left_unhandled       0      ← hard target
irreversible_actions_auto_committed        0      ← hard target
high_vulnerability_zones_uncovered         0
mean_in_cone_coverage                      1.0
correct_abstentions                        3/3
plans_rejected_by_critic_before_commit     129
mean_iterations_to_accepted_plan           1.76
mean_confidence_when_accepted              0.721
wall_clock_seconds                         0.32
```

"Correct abstention" means a run with a missing **core** source (advisory, zones, shelters, staging) declined to plan rather than inventing a number. A missing **optional** source (hospitals, road closures, declarations) degrades instead: the plan is made and the trace records what was unavailable.

Full report: [`output_examples/eval_report.json`](output_examples/eval_report.json).

---

## API

| Route | Purpose |
|---|---|
| `POST /run` | Plan one replay hour, or an N-hour Markovian replay |
| `POST /events` | Inject `advisory_update`, `shelter_capacity` or `road_closure`; returns changed beliefs, invalidated vs preserved decisions, the revised plan and a diff |
| `POST /runs/{id}/decisions` | An operator approves or declines a held action (`{"action_id", "decision": "approve" or "decline", "note"}`); recorded in the World Model, the trace and H-MEM; only escalated actions can be decided |
| `GET /runs/{id}/decisions` | The operator's decisions on the current plan and how many held actions are still pending |
| `GET /runs/{id}` | Plan, escalations, operator decisions, recalled memory, world digest |
| `GET /runs/{id}/trace` | The JSONL trace |
| `GET /runs/{id}/world` | Full World Model snapshot: beliefs with provenance, decision graph, conflicts |
| `GET /health` | Zetaris and Nightshift reachability, pinned lane models |
| `GET /` | Operator console: plan, map, change-of-mind diff, approve or decline held actions |

Failures return a structured error with `type`, `message` and `recoverable` — never a stack trace, never a hang.

### Examples

| Input | What it shows |
|---|---|
| [`example_1.json`](input_examples/example_1.json) | Five counties at advisory 15. 29 actions, 2 iterations, 11 escalations. Pair with [`event_track_shift.json`](input_examples/event_track_shift.json) for the change of mind |
| [`example_2.json`](input_examples/example_2.json) | Lee + Charlotte only, 4 trucks. 7 actions — a narrower request plans narrower |
| [`example_3.json`](input_examples/example_3.json) | 24-hour replay across the forecast shift. Plan changes at step 19; context stays flat |

Outputs in [`output_examples/`](output_examples/), produced by real runs. A sample trace is committed at [`logs/trace-sample.jsonl`](logs/trace-sample.jsonl).

---

## Tracing

One file per run at `logs/trace-<run_id>.jsonl`, one JSON object per line: every agent step, handoff, critique, rejection, retry, fallback, Zetaris query with row count, engine operation, the model behind every call, and every escalation. Secrets are redacted on the way in, not on the way out ([`app/tracing.py`](app/tracing.py)).

---

## Tests

```bash
./.venv/bin/python -m pytest tests/ -q      # 34 passed
```

Engine conformance (concurrent writes, stale-write protection, cross-session recall, consolidation, 24-step bounded context, one-time H-MEM injection), plus: every action cites evidence that exists · Critic rejection produces a measurably better revision · the loop stops at the cap and still returns its best plan · an evacuation order never auto-commits · low confidence escalates even reversible actions · the Belief Reviser invalidates only dependents · three examples differ meaningfully · SAMPLE_MODE runs with empty credentials and never reads `output_examples/` · failures are structured.

---

## Limitations

Stated plainly, because an emergency manager deserves to know what this is not.

- **Not an official decision tool.** A research prototype built in a hackathon. Do not use it to make real evacuation decisions.
- **Runs live in process memory.** `RUNS` in `app/orchestrator.py` is a dict, so a run, its events and its operator decisions last as long as the process does. On the hosted demo that is one serverless instance; a later request can land on another one and get a 404 for the run. The console handles that by rebuilding the run from the same request and retrying once, and says so. A durable run store is the fix, not done here.
- **Scoped to five Florida counties and one storm.** The planner, simulator and belief reviser never mention a place, and `region` is a request parameter (`WHERE county IN (...)` in `app/queries.py`). But every Zetaris table only holds the Ian replay: Lee, Charlotte, Sarasota, Pinellas and Hillsborough. Ask for another county and you get zero rows and an abstention. The live registrations carry a Florida filter in their endpoint URLs too (`scripts/zetaris/05_disaster_declarations.sql`, `07_storm_warnings_archive.sql`). Going to a second state needs one real change before any new data: the app keys on county *name*, and there are about a dozen Lee Counties in the US, so every table and query would switch to state plus county FIPS.
- **The replay uses archived data.** Advisory positions are rounded and cones simplified. Real NHC products are more precise.
- **Shelter occupancy during Ian is simulated.** `data/replay/facilities.json` is synthetic, generated to FEMA- and ACS-shaped distributions. No real shelter occupancy or patient data appears anywhere in this repository.
- **The 25-mile evacuation buffer under-covers on purpose.** A major hurricane's damaging wind field reaches far beyond 25 miles of the forecast cone. Pulsar 5 deliberately does not order evacuations out there; it surfaces those zones to the operator as `distant_vulnerable_zone` instead.
- **The vulnerability score is a simple equal-weight composite** of four ACS signals (`vulnerability_score` in `app/agents/analyst.py`), chosen to be auditable rather than optimal. It is not CDC SVI.
- **18% shelter-seeking rate** is a flat planning rule of thumb. Real rates vary hugely with storm category, evacuation zone and messaging.
- **Live ArcGIS and IEM endpoints need verifying before a live demo.** Field names differ between NSS layers; the IEM service shape should be checked against a live response. SAMPLE_MODE does not depend on either.
- **No authentication on the API.** It binds `0.0.0.0` for the demo. Do not expose it.
- **Road closures are synthetic** and routing is straight-line distance, not road network distance.

---

## Layout

```
app/
  main.py            FastAPI routes
  orchestrator.py    the loop, bounded iteration, escalation, H-MEM feedback
  agents/            analyst planner simulator critic risk belief_reviser
  adapters/          zetaris.py (only data path) · nightshift.py (only model path)
  engines/           world_model/ hmem/ markovian/
  sim/               checks.py (deterministic verdict) capacity.py geo.py
  queries.py         named Lightning SQL + the predicates each one declares
  tracing.py         JSONL tracing with secret redaction
scripts/
  zetaris/*.sql      registration DDL, one file per source
  register_sources.py  dry-run by default, one statement at a time
  build_replay.py    regenerates replay data and the SAMPLE_MODE cache
  eval.py            reliability scoreboard
ui/index.html        operator console: map, plan, evidence, change-of-mind diff
```

Dependencies: Python 3.11, FastAPI, uvicorn, httpx, pydantic, pytest. No shapely — the geometry we need is 40 lines in `app/sim/geo.py`.

---

## Honesty about what this repository is

Built fresh for this hackathon; the first commit is dated after 22 Oct 2026. No pre-existing code. The conversation that set up the environment is kept at [`docs/session-log.md`](docs/session-log.md).
