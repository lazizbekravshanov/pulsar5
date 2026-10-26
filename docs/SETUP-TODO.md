# What's left on this machine

Checked 26 Oct. Four things stand between the repo and a submission that meets the hard requirements.

---

## 1. Free disk — blocking the Zetaris install

The install wants **15 GB free**. You have **12 GB** on a 460 GB disk that is 98 % full.

The hogs are regenerable caches, not your projects. This clears about **25 GB** and touches nothing you cannot re-download:

```bash
rm -rf ~/Library/Caches/com.todesktop.230313mzl4w4u92.ShipIt ~/Library/Caches/com.github.GitHubClient.ShipIt ~/Library/Caches/com.microsoft.VSCodeInsiders.ShipIt ~/Library/Caches/pip ~/Library/Caches/go-build ~/Library/Caches/rattler ~/.cache/uv ~/.cache/prek ~/.cache/pre-commit
```

What that is: three app-updater staging folders (pure installer leftovers), the pip / Go / rattler / uv package caches, and the pre-commit hook environments. All rebuild on next use.

**Deliberately not in that command:**

| Kept | Size | Why |
|---|---|---|
| `~/.cache/huggingface` | 8.8 G | ML model weights — slow to re-download, and you may be mid-project |
| Docker images | 19.9 G | `sql2022` and `sqlserver` have been up 10 hours |
| Browser caches | ~3 G | your profiles |

If you still need more afterwards, `~/Downloads` holds 8 GB including a 929 MB RStudio dmg and a 554 MB Docker dmg you have already installed.

---

## 2. Zetaris — not installed

Everything here needs your account, so none of it could be done for you.

1. Register at `cloud.enterprise.zetaris.com/register`
2. Download `zetaris-platform.zip` from the dashboard, and copy the registry sign-in command
3. `unzip`, then `docker login zetarishackathon.azurecr.io`, then `./preflight.sh`
4. Edit `zetaris-platform/.env` **before first start** — set `ZETARIS_ADMIN_ORG=Pulsar5`, your email, a password with 8+ chars including one of `$ @ ! % * ? &`. That file belongs to Zetaris; never copy it into this repo.
5. `docker compose pull && docker compose up -d` (~4 GB, 30–45 min), then `./verify.sh` — expect 9/9
6. Sign in at `http://localhost:3000`, run `SELECT 1 AS one;`
7. **Create the API key yourself** in the web UI and copy it immediately. The hackathon rules say never to ask an AI agent to create or fetch it.

Docker is already running with 10 CPUs and 7.65 GiB usable, which is 8 GB minus VM overhead — fine as-is.

Then, from this repo:

```bash
cp .env.example .env          # fill in ZETARIS_API_KEY and the Nightshift token
./.venv/bin/python run.py --check                    # should report both reachable
./.venv/bin/python scripts/register_sources.py       # dry run first — read the output
./.venv/bin/python scripts/register_sources.py --apply --verify
```

The raw-URL tables resolve now that the repo is public — all three data files return HTTP 200.

**Two sources to check against a live payload before recording:** FEMA NSS field names differ between layers (`scripts/zetaris/02_shelters.sql`), and the IEM service shape should be confirmed (`07_storm_warnings_archive.sql`). Both have VERIFY notes in the SQL.

---

## 3. Nightshift — running, but the NVIDIA key was refusing

The runtime is up: `/healthz` returns 200, `/v1/models` returns 401 without a token, which is correct.

Your session log says a real chat call to `nvidia/nemotron-3.5-lightning-30b-a3b` came back **502** — Nightshift reached NVIDIA and NVIDIA rejected the key. A model list can show Nemotron names even when the key cannot call them.

1. Generate a fresh key at `build.nvidia.com/models` on an account that can use Nemotron
2. Nightshift → **Setup → Providers → NVIDIA**, paste, save, refresh the model list
3. Console (top right) → copy the Runtime URL and Runtime API token into this repo's `.env`
4. Pin the three lanes from whatever `run.py --check` reports as Nemotron-backed:

```env
METERLESS_MODEL_FAST=nvidia/nemotron-3.5-lightning-30b-a3b
METERLESS_MODEL_DEEP=nvidia/nemotron-3-ultra-550b-a55b
METERLESS_MODEL_JUDGE=nvidia/nemotron-3.5-bolt-e4b
```

Those three ids are the ones your catalogue listed last time. The setup guide's suggested deep model, `nemotron-3.5-super-vl-120b-a12b`, was not among them.

**The key never goes in this repo, this README, the deck, or the video.**

Verify with one call:

```bash
./.venv/bin/python run.py --check
SAMPLE_MODE=false ./.venv/bin/python run.py --input input_examples/example_1.json \
  | grep -o '"models_used":[^]]*]'
```

If that prints `nvidia/nemotron-…` rather than `meterless/auto`, the sponsor requirement is genuinely met and shot 6 of the video is recordable.

---

## 4. HackOS submission

Not started. Remus's advice is to create it today even unfinished, because it stays editable.

**Add every teammate before you create it — the roster locks on creation.** That is the one irreversible step in this whole list, which is why it was left for you.

Everything to paste is in [SUBMISSION.md](SUBMISSION.md).
