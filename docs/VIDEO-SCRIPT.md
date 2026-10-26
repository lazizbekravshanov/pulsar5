# Demo video — read-aloud script

**Target 3:00.** Hard limits 1:00 to 4:00. About 450 words of narration, which is a comfortable pace.

Read the **SAY** lines out loud. They are written the way people speak, so do not polish them on the fly. Short sentences on purpose: they survive a nervous take.

---

## Before you hit record

```bash
cd ~/pet/pulsar5
rm -f data/hmem/store.json          # so "recalled from earlier runs" starts empty
docker compose -f ~/pet/zetaris-platform/docker-compose.yml ps   # confirm Zetaris is up
./.venv/bin/python run.py --check   # both services must say reachable: true
./.venv/bin/python run.py           # serves on :8000
```

Open side by side, both at a readable font size:
- **Left:** terminal
- **Right:** browser at `http://localhost:8000`, window at least 1500 px wide so the UI keeps three columns

Record at 1080p or better. Zoom the terminal font up two steps. Judges watch these small.

**Do not record `example_3` live.** It makes about 144 model calls and your NVIDIA account has a per-minute cap. Run that one in SAMPLE_MODE if you show it at all.

---

## Animated inserts

Three silent MP4s, 1920x1080, rendered and ready to drop into the edit. Sources and renders live in `~/pet/videos/pulsar5-inserts/` (outside this repo so `node_modules` stays out of it).

| File | Length | Drop at |
|---|---|---|
| `renders/title-card.mp4` | 4.5 s | 0:00, before you speak |
| `renders/architecture.mp4` | 8.0 s | ~1:05, under the loop explanation |
| `renders/stat-reveal.mp4` | 6.0 s | ~2:00, right after the track-shift click |

They have no audio, so your narration runs straight over them. Re-render any of them with:

```bash
cd ~/pet/videos/pulsar5-inserts
cp src/<name>.html index.html && npm run check && npx hyperframes render . -o renders/<name>.mp4
```

---

## 0:00 – 0:18 · The problem

**ON SCREEN:** `title-card.mp4` (4.5 s), then deck slide 6 (*The forecast was not wrong*).

> **SAY:** Hurricane Ian killed a hundred and fifty six people and cost a hundred and twelve billion dollars. Storm surge killed forty one, and thirty six of those were in one county.
>
> Here is the part that matters. The National Hurricane Center's own report says the forecast cone contained the landfall location for every single advisory. The data was there the whole time. The plans built on it went stale.

---

## 0:18 – 0:35 · Run it

**ON SCREEN:** Terminal. Type and run:

```bash
curl -X POST localhost:8000/run -H 'Content-Type: application/json' -d @input_examples/example_1.json
```

> **SAY:** Pulsar 5 is a planning agent for a county emergency manager. Everything is reachable over HTTP, so you can curl it. This is advisory fifteen, five Florida counties, about forty eight hours before landfall.

---

## 0:35 – 1:00 · The plan

**ON SCREEN:** Switch to the browser. Map fills in, plan appears on the right. Scroll one action card into view so the evidence pills are readable.

> **SAY:** Twenty nine actions. Every one of them cites World Model fact ids, so you can trace any line back to the row it came from.
>
> Eleven are held for a human and eighteen commit automatically. The split is not about confidence. An evacuation order is irreversible, so it never commits on its own, no matter how sure the system is.

---

## 1:00 – 1:25 · The loop

**ON SCREEN:** `architecture.mp4` (8 s) over the first half, then cut to the terminal or trace panel. Scroll to `reject_plan`, then `accept_plan`.

> **SAY:** This is the part I care about most. It is a loop, not a chain.
>
> The Planner opens three shelters first, because opening a shelter costs staff and cots. The Simulator does the arithmetic in plain code: fourteen thousand expected evacuees against five thousand spaces. The Critic refuses and sends the shortfall back.
>
> Second pass opens more and reaches twenty three thousand. Accepted. Two iterations, not three. It stops when it is done.

---

## 1:25 – 1:40 · Zetaris

**ON SCREEN:** Trace lines showing `zetaris_query` with `row_count` and `data_source`.

> **SAY:** Every fact came through Zetaris. Seven queries, each row stamped with its source table and the SQL that produced it. There is no file read and no direct API call anywhere in the application. You can check that by grepping the repo.

---

## 1:40 – 1:55 · Nightshift and Nemotron

**ON SCREEN:** Trace lines showing `model=nvidia/nemotron-...`.

> **SAY:** Every model call goes to the Meterless Nightshift runtime, and the model that served it is recorded on the line. Three separately pinned Nemotron lanes: lightning for the Analyst, ultra for the Planner, bolt for the Critic.
>
> There is no OpenAI or Anthropic client in this codebase at all.

---

## 1:55 – 2:30 · The change of mind

**ON SCREEN:** Click **Advisory 23 — track shifts south**. Let the map redraw, then cut to `stat-reveal.mp4` (6 s) as you say the numbers.

> **SAY:** Now the world changes. Advisory twenty three arrives and the track moves south.
>
> Twenty seven beliefs change. Eleven decisions are invalidated, and only eleven. Thirty six are preserved, because the Belief Reviser walked the decision graph and found that nothing those decisions relied on had actually moved.
>
> Lee County enters with all three zones. Hillsborough stands down. And Sarasota sits in both cones, so nothing touched it. That is the difference between revising a plan and starting over.

---

## 2:30 – 2:45 · Engines and reliability

**ON SCREEN:** Terminal: `./.venv/bin/python scripts/eval.py | tail -20`

> **SAY:** Three Meterless engines do real work here. The World Model survives eight threads writing at once with nothing lost. H-MEM carries lessons between runs. Markovian holds context flat at four hundred and fifty characters across twenty four replay steps.
>
> Thirty five perturbed scenarios. Zero unhandled violations. Zero irreversible actions committed without a human.

---

## 2:45 – 3:00 · Close

**ON SCREEN:** Deck slide 17 (the Lee County line).

> **SAY:** One last thing. At advisory fifteen, when every forecast pointed at Tampa, Pulsar 5 had already flagged Lee County to the operator. Vulnerability zero point eight three, forty eight miles outside the cone, no evacuation warranted yet, but somebody should decide about pre-positioning.
>
> Lee County is where Ian made landfall.
>
> I am not claiming it predicted the storm. It refused to let a vulnerable area disappear from the plan just because the current cone missed it, and it handed that call to a human. That is the whole idea. Thank you.

---

## Honesty checks before you upload

Watch it once and confirm each of these, because a judge will.

- [ ] The trace on screen says `data_source: zetaris`, not `sample_cache`. If Zetaris was down, say out loud that this is SAMPLE_MODE against recorded results.
- [ ] The trace shows `model=nvidia/nemotron-...` on lines **without** `status: fallback`. Never narrate Nemotron over a fallback line.
- [ ] Nothing on screen shows an API key, a token, or a `.env` with real values. Check the terminal scrollback before you start.
- [ ] Runtime is between 1:00 and 4:00.

## If something breaks mid-take

Keep going and say what happened. A demo where the presenter says "that is the rate limit on our NVIDIA trial account, here is the fallback path working as designed" reads as competence. A silent cut reads as a cover-up.
