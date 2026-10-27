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

## 2. Demo video

**Rendered: `~/pet/videos/pulsar5-demo/renders/pulsar5-demo.mp4`, 1920x1080, 3:24, narrated.** Inside the 1:00 to 4:00 window.

Published as a release asset so the form can take a link: https://github.com/lazizbekravshanov/pulsar5/releases/download/v0.1-demo/pulsar5-demo.mp4 (release page: https://github.com/lazizbekravshanov/pulsar5/releases/tag/v0.1-demo).

It is an animated walkthrough built with HyperFrames from the approved script in [VIDEO-SCRIPT.md](VIDEO-SCRIPT.md), not a screen recording. Nine scenes: the problem with the NHC figures, the curl call, the plan on the real console, the Planner/Simulator/Critic loop, Zetaris, Nightshift, the change of mind on the real console, the three engines plus the scoreboard, and the Lee County close. The three short inserts (title card, architecture, stat reveal) are cut in.

What a judge should know about how it was made, so nothing on screen overclaims:

- **The console frames are real.** Captured at 1920x1080 from the public deployment, pulsar5-nine.vercel.app, by driving the actual page. That deployment runs SAMPLE_MODE, so its header reads "Zetaris from cache / Nightshift recorded" and the plan panel says "Models that answered: none, fell back". The narration does not claim Nemotron over those frames.
- **The Zetaris and Nightshift trace lines are from a real live run**, `logs/trace-ui-2022-09-26T12_00Z-5c-1793069641774.jsonl`: seven queries with `data_source=zetaris`, and the three lanes served by `nvidia/nemotron-3.5-lightning-30b-a3b`, `nvidia/nemotron-3-ultra-550b-a55b` and `nvidia/nemotron-3.5-bolt-e4b` with `status=success` and no fallback.
- **The loop numbers** (14,184 vs 5,441, Critic 0.56 then 0.78, 11 of 29 held) are from run `ian-landfall-minus-48h`, the same run the deck's loop slide cites.
- **The narration is a synthesized voice** (macOS, Samantha). No teammate's voice was recorded.
- Every impact figure is sourced in [IMPACT-DATA.md](IMPACT-DATA.md).

Regenerate after any change with `cd ~/pet/videos/pulsar5-demo && python3 narrate.py && python3 build.py && npm run check && npx hyperframes render . -o renders/pulsar5-demo.mp4`.

### Project summary, for the form

**One line.** A disaster response planning agent that changes its mind correctly when the world changes.

**Short description.** Pulsar 5 plans shelter openings, supply staging and evacuation priority for a county emergency manager in the hours before a hurricane, replayed against Hurricane Ian 2022. Every fact enters through Zetaris with its source table, SQL and timestamp. A Planner proposes, a plain-code Simulator checks capacity and coverage, a Critic can reject and send the shortfall back, and a Risk agent holds every irreversible action for a human. When a new advisory arrives, a Belief Reviser walks the decision dependency graph and invalidates only the decisions that cited a fact that actually changed: at advisory 23, 27 beliefs change, 11 decisions are invalidated and 36 are preserved. Three Meterless engines carry the state (World Model), the memory across runs (H-MEM) and the bounded hourly replay (Markovian); every model call goes through Nightshift to a pinned Nemotron lane. Scoped to five Florida counties and one storm; the code is region-free, the registered data is not.

---

## 3. Form checklist

- [ ] Teammates added **before** creating the submission (roster locks): Lazizbek, Anish Posim Reddy, Balaji Moka
- [ ] Submission created early, even unfinished
- [x] Public GitHub repo with README: https://github.com/lazizbekravshanov/pulsar5
- [x] Live demo judges can open: https://pulsar5-nine.vercel.app
- [x] Sponsor technology explanation (section 1 above)
- [x] Slide deck as PDF: `docs/pulsar5-deck.pdf` in the repo (the artifact link is private and 403s for anyone else)
- [x] Demo video, 3:24: https://github.com/lazizbekravshanov/pulsar5/releases/download/v0.1-demo/pulsar5-demo.mp4 (local file at `~/pet/videos/pulsar5-demo/renders/pulsar5-demo.mp4` if the form wants an upload)
- [x] Zetaris running locally, eight views registered, `verify.sh` 9/9
- [x] Nightshift NVIDIA key working, three lanes pinned and serving
- [ ] Submitted well before 23:45 UTC


- [ ] Teammates added **before** creating the submission (roster locks)
- [ ] Submission created early, even unfinished
- [x] Public GitHub repo with README
- [x] Sponsor technology explanation (section 1 above)
- [x] Slide deck
- [ ] Demo video, 1–4 min
- [ ] Zetaris running and sources registered for real
- [ ] Nightshift NVIDIA key working, three lanes pinned
- [ ] Submitted well before 23:45 UTC
