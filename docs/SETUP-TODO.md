# Setup status

Updated 26 Oct 2026, after a full local Zetaris install.

| | Status |
|---|---|
| Zetaris installed and running | **DONE** — `verify.sh` 9/9 |
| All 7 sources registered in Zetaris | **DONE** — 8 views, all returning rows |
| Pulsar 5 running live against Zetaris | **DONE** — traces read `"data_source": "zetaris"` |
| Nightshift NVIDIA key | **BLOCKED** — still refused by the provider |
| HackOS submission | **NOT STARTED** |
| Demo video | **NOT STARTED** |

---

## Running it live (for teammates)

Zetaris runs locally, so each of you needs your own install. The committed examples pin `sample_mode: true` so anyone can run them with no credentials at all:

```bash
./.venv/bin/python run.py --input input_examples/example_1.json
```

To exercise the real Zetaris path, add `--live` (overrides the input file's `sample_mode`):

```bash
./.venv/bin/python run.py --check                               # both services
./.venv/bin/python run.py --input input_examples/example_1.json --live
```

Live and SAMPLE_MODE produce the **same 29-action plan**, which is the point — the cache is faithful, not a demo branch. Live takes ~25 s against real HTTP; the cache takes 5 ms.

### Installing Zetaris yourself

1. Register at `cloud.enterprise.zetaris.com/register`
2. Download page → step 1 → **click the eye icon before the copy icon**. The sign-in command is masked by default, and copying without revealing gives you bullet characters that fail authentication. This cost us an hour.
3. `docker login` with that command, then:

```bash
cd ~/pet && unzip zetaris-platform.zip && cd zetaris-platform
# set ZETARIS_ADMIN_ORG=Pulsar5 BEFORE first start - it cannot be changed after bootstrap
./preflight.sh        # 2 warnings on Apple Silicon are expected, not failures
docker compose pull   # ~4 GB
docker compose up -d
./verify.sh           # bootstrap takes ~2 min to load sample data; re-run if it is still going
```

4. UI → **Management → API Tokens** → create a key (shown in plaintext once)
5. `cp .env.example .env` in this repo, paste the key into `ZETARIS_API_KEY`
6. `python scripts/register_sources.py --apply --verify --skip-exists`

Needs ~15 GB free disk and 8 GB given to Docker.

---

## What live mode taught us

Three things only showed up against a real instance, all now fixed in the repo.

**The SQL endpoint needs more than `{"sql": ...}`.** It returns 400 with *"Missing required field at 'queryId', Missing required field at 'source'"*. The real body is:

```json
{"queryId": "<uuid4>", "sql": "...", "source": "SqlEditor", "limit": 5000}
```

Responses come back as `{"headers": [...], "data": [[...]], "total": n}` — columns are under `headers`, not `columns`, and every value is a string.

**Table references must be qualified.** `FROM storm_advisories_raw` fails with `TABLE_OR_VIEW_NOT_FOUND`; it has to be `FROM PULSAR5_REST.storm_advisories_raw`.

**Two live upstreams do not work as documented:**

- **Census ACS** now requires an API key. The response is an HTML page titled "Missing Key" that lands in Spark as a single `_corrupt_record` column. SETUP.md's "key optional at low volume" is out of date.
- **FEMA NSS ArcGIS** answers with HTTP 200 but an empty feature set — the `where=state='FL'` predicate matches nothing, so that layer doesn't carry a `state` field by that name. Fixing it needs a field listing from the live service.
- **NASA NCCS HIFLD mirror** would not register as a REST table at all.

Those three now read committed extracts served from this repo's raw GitHub URL and registered as Lightning REST tables — still registered in Zetaris, still queried through its REST API, and the data is labelled synthetic in `data/replay/` and the README. The live attempts are kept in `scripts/zetaris/*.sql` as comments with the exact errors, so they can be revived with a Census key and an ArcGIS field listing.

`disaster_declarations` also returns 0 rows live (OpenFEMA's `designatedArea` is formatted `"Lee (County)"`, not `"Lee"`). It is an optional source, so the run degrades and records the gap rather than abstaining.

---

## Still blocking the submission

### 1. Nightshift NVIDIA key — mandatory

Remus's correction made Nightshift a **hard requirement**, not optional. The runtime is up (`/healthz` 200) but a real chat call returns **502**: Nightshift reached NVIDIA and NVIDIA refused the key.

1. New key at `build.nvidia.com/models` on an account that can use Nemotron
2. Nightshift → **Setup → Providers → NVIDIA**, paste, save, refresh models
3. Console → copy the Runtime URL and token into this repo's `.env`
4. Pin the lanes from whatever `run.py --check` reports as Nemotron-backed

```env
METERLESS_MODEL_FAST=nvidia/nemotron-3.5-lightning-30b-a3b
METERLESS_MODEL_DEEP=nvidia/nemotron-3-ultra-550b-a55b
METERLESS_MODEL_JUDGE=nvidia/nemotron-3.5-bolt-e4b
```

Until this works, `models_used` reads `meterless/auto` and every agent logs `"status": "fallback"`. **Do not narrate Nemotron over a fallback line in the video.**

### 2. HackOS submission

Create it early even unfinished — it stays editable. **Add every teammate first: the roster locks on creation.** Everything to paste is in [SUBMISSION.md](SUBMISSION.md).

### 3. Demo video

Nine-shot script with timings in [SUBMISSION.md](SUBMISSION.md). Shot 5 now genuinely shows `data_source: zetaris`.
