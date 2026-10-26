# Submission pack — Pulsar 5

Everything the HackOS form asks for, ready to paste. Deadline **Tue 27 Oct, 23:45 UTC**.

Repo: https://github.com/lazizbekravshanov/pulsar5 (public)

> **Create the submission early, even unfinished — it stays editable until the deadline.
> Add every teammate FIRST: the roster locks the moment the submission is created.**

---

## 1. Sponsor technology explanation

*Paste into the sponsor tech field. Trim the headings if the form is one box.*

### Zetaris — the data layer

Zetaris is the only way data enters Pulsar 5. Seven sources are registered as Lightning REST tables and queried through `POST /sql-editor/sqls/run`: storm advisories, FEMA shelters, HIFLD hospitals, Census ACS vulnerability, staging sites, road events and FEMA disaster declarations. Nothing in `app/` opens a data file or calls NWS, FEMA or Census directly — that is checkable by grep.

Registration is scripted rather than clicked. `scripts/zetaris/*.sql` holds one DDL file per source following the getting-started PokéAPI pattern, and `scripts/register_sources.py` sends them **one statement at a time**, dry-running by default.

Every row Zetaris returns becomes a World Model belief carrying its source table, the exact SQL that produced it, and an `as_of` timestamp. That provenance is what lets the Planner cite evidence ids and the Critic flag staleness per source.

One decision worth naming: the NWS alerts API requires a `User-Agent` header that a Lightning REST table cannot send. Rather than bypass Zetaris with a direct HTTP call, we moved that source to the Iowa Environmental Mesonet archive, which needs no header and reaches back to 2002 — a better fit for a 2022 replay anyway.

### Meterless Engines — three, all load-bearing

**World Model** (`app/engines/world_model/engine.py`) is the shared state every agent reads and writes: beliefs with provenance, decisions with dependencies, and a conflict log. Concurrent writes merge per key, and a late write carrying an older `as_of` is recorded as a conflict instead of clobbering fresher data. The conformance test runs 8 threads × 60 writes and loses none.

**H-MEM** (`app/engines/hmem/engine.py`) is memory across runs on three levels: L0 episodes, L1 lessons promoted after repeats, L2 standing operator policy. It is wired so a recalled lesson changes the plan rather than the prompt: a lesson that a shelter fills faster than its headroom suggests writes a `memory_derate` onto that shelter, and both the Planner and the Simulator read the same `app/sim/capacity.py` function. If those two disagreed, the lesson would be decoration.

**Markovian** (`app/engines/markovian/engine.py`) drives the hourly replay. Each hour is one bounded step with a compressed carryover, and H-MEM context is injected once at step 1 per the COMBINE contract. Across 24 steps the context stays flat at 448–452 characters while each step returns roughly 20 KB of notes.

### Meterless Nightshift — the only model provider

There is no `openai`, `anthropic` or NVIDIA client anywhere in this codebase. Every model call is an OpenAI-compatible POST to the Nightshift Runtime URL with `meterless-app: pulsar5` and `meterless-scope: user` headers, implemented in `app/adapters/nightshift.py`.

Three lanes are pinned separately so a cheap route never silently does the Critic's job:

| Lane | Agents |
|---|---|
| fast | Analyst tool calls, Simulator narration, Risk classification |
| deep | Planner, Belief Reviser |
| judge | Critic |

The model that served each request is recorded on every trace line, which is the audit trail for the NVIDIA award:

```json
{"agent_name":"Critic","action":"reject_plan","model":"nvidia/nemotron-3.5-bolt-e4b",
 "lane":"judge","confidence":0.56,"status":"rejected"}
```

When Nightshift is unreachable the adapter returns `fallback_used=True` with no text and each agent runs its own deterministic path, logged explicitly as `"status":"fallback"`. Agents never fabricate a model answer.

### NVIDIA Nemotron

Reached only through Nightshift, never called directly. The NVIDIA API key lives inside the Nightshift app and is never read by this process; `app/tracing.py` redacts secrets on the way in. `python run.py --check` lists which catalogue routes are Nemotron-backed so the three lanes can be pinned to them.

---

## 2. Demo video — script and shot list

**Target 3:00. Hard limits 1:00–4:00.** Remus asks for four things on screen: the main workflow, Zetaris being queried, a Meterless Engine at work, and one real request through Nightshift.

Record at 1500×950 or wider so the three-column UI does not collapse to one column.

### Before you hit record

```bash
cd ~/pet/pulsar5
rm -f data/hmem/store.json          # so "recalled from earlier runs" starts empty
./.venv/bin/python run.py           # serves on :8000
```

Open http://localhost:8000 and a terminal side by side.

### Shot list

| # | Time | On screen | Say |
|---|---|---|---|
| 1 | 0:00–0:20 | Slide 6 (*The forecast was not wrong*) | "Hurricane Ian killed 156 people and cost $112.9 billion. Storm surge killed 41 — and 36 of those were in one county. But the National Hurricane Center's own report says the cone contained the landfall location for every advisory cycle. The data was there the whole time. The plans built on it went stale." |
| 2 | 0:20–0:35 | Terminal: `curl -X POST localhost:8000/run -d @input_examples/example_1.json` | "Everything is reachable over HTTP — judges can curl it. This is advisory 15, five counties." |
| 3 | 0:35–1:05 | UI: the map fills, plan appears on the right | "29 actions. Every one cites World Model fact ids. 11 held for a human, 18 auto-committed." |
| 4 | 1:05–1:30 | Scroll the trace panel to the `reject_plan` line, then `accept_plan` | **The loop.** "First pass opens 3 shelters — 5,441 spaces against 14,184 evacuees. The Critic refuses at 0.56 and sends the shortfall back. Second pass reaches 23,556 and is accepted." |
| 5 | 1:30–1:45 | Trace lines showing `zetaris_query` with `row_count` | **Zetaris.** "Seven queries, every row stamped with its source table and SQL. Nothing else touches data." |
| 6 | 1:45–2:00 | Trace lines with `"model":"nvidia/nemotron-…"` | **Nightshift.** "Every call goes to the Nightshift runtime, and the model that served it is on the line. Three Nemotron lanes." |
| 7 | 2:00–2:35 | Click **Advisory 23 — track shifts south** | **The change of mind.** "27 beliefs change. 11 decisions invalidated, 36 preserved. Lee County enters, Hillsborough stands down. Sarasota was in both cones, so nothing touched it." |
| 8 | 2:35–2:50 | Terminal: `./.venv/bin/python scripts/eval.py \| tail -20` | **The engines + scoreboard.** "35 perturbed scenarios. Zero unhandled violations, zero irreversible auto-commits, 3 of 3 correct abstentions. Markovian holds context flat across 24 steps." |
| 9 | 2:50–3:00 | Slide 15 (the Lee County line) | "At advisory 15, with every forecast pointing at Tampa, it had already flagged Lee County to the operator. That's the Critic refusing to let a vulnerable area vanish — and handing the call to a human." |

Every figure in shot 1 is sourced in [IMPACT-DATA.md](IMPACT-DATA.md), straight from the NHC post-storm report.

### Two warnings

- **Shot 6 needs a working NVIDIA key in Nightshift.** Until then `models_used` reads `meterless/auto` and the trace says `"status":"fallback"`. Do not narrate Nemotron over a fallback line — a judge will read it.
- **Shot 5 currently shows `"data_source":"sample_cache"`.** With Zetaris running it reads `"zetaris"`. If Zetaris is not up by record time, say plainly that the demo is in SAMPLE_MODE against recorded Zetaris results rather than glossing over it.

---

## 3. Form checklist

- [ ] Teammates added **before** creating the submission (roster locks)
- [ ] Submission created early, even unfinished
- [x] Public GitHub repo with README
- [x] Sponsor technology explanation (section 1 above)
- [x] Slide deck
- [ ] Demo video, 1–4 min
- [ ] Zetaris running and sources registered for real
- [ ] Nightshift NVIDIA key working, three lanes pinned
- [ ] Submitted well before 23:45 UTC
