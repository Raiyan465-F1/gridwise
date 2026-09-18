# HANDOFF.md — what's done, how it works, how to run and ship it

For the teammate who will run, deploy and submit this. Read this file top to bottom once; it takes about 5 minutes.

## 1. Status at a glance

| Area (rubric points) | Status |
|---|---|
| API contract & schema (10) | ✅ Built and tested. Exact endpoints and fields; 400 / 422 / controlled 500. |
| Directive application & constraints (25) | ✅ Every directive is enforced by the LP; independent replay validator passes on all samples and 468 random feasible cases. |
| Optimization quality (10) | ✅ Global LP optimum; equals the reference cost on 10/10 samples and an independent formulation on 500 fuzz cases. |
| LLM interpretation (25) | ✅ Built; ⚠️ **not yet measured with a real LLM key** (sandbox had none). Both provider code paths verified end to end against a mock LLM server. Run `scripts/eval_interpreter.py` first thing (step 3 below). |
| Performance & reliability (10) | ✅ Optimizer ~10 ms; load-tested 100 requests × 20 concurrent with a slow, flaky mock LLM: all 200, all valid. Hung provider still answers in ~16 s. Real p95 depends on the LLM (Haiku is typically 1–3 s). |
| Deployment & Docker (10) | ⏳ Dockerfile + CI workflow written; **needs a device**: GitHub repo, image build/push, hosting. |
| Documentation (10) | ✅ README with every rubric-required section; quickstart dry-run verified. Fill in `<REPO_URL>`, `<OWNER>/<REPO>:<TAG>`. |
| 3-minute video (tie-break) | ⏳ Script in `docs/VIDEO_SCRIPT.md`; needs recording. |

## 2. What the service does

It exposes `GET /health` and `POST /optimize-energy`. For each request it:

1. **Validates the request** (`app/schemas.py`): 24 unique hours 0–23, 1–3 non-empty notes, numeric finite fields, battery sanity. Structural problems → 400, impossible values → 422.
2. **Interprets the notes with an LLM** (`app/llm.py`, `app/prompt.py`): one call for all notes, temperature 0, forced tool call (Claude's structured-output mechanism) so the model returns JSON. The model outputs an *intermediate* form per note: directive type, start hour, **exclusive** end hour, value as stated, and the value's unit.
3. **Applies deterministic guardrails** (`app/normalize.py`): builds the hours array in code (handles "until midnight" and overnight wrap), converts units ("80% reduction" → 0.2, "50% of capacity" → kWh), enforces every rule in Problem Statement §8, and forces `no_op` ⇔ `applies=false` + `null`.
4. **Checks feasibility** and **recovers** (`app/interpreter.py`): if the LLM output is invalid or makes the schedule impossible, it retries once with the validator's error message; if the provider is down or still wrong, it uses the rule-based backup parser (same guardrails); if a single note still can't be interpreted safely, only that note becomes `no_op`. Validated interpretations are cached (LRU) so repeated notes skip the LLM.
5. **Optimizes** (`app/optimizer.py`): a linear program (SciPy HiGHS) minimizing Σ tariff × grid subject to energy balance, solar limit, battery bounds and rate limits, end-of-day neutrality and all directives. One net battery variable per hour is exact because there are no losses, and it guarantees one action per hour.
6. **Cleans the output**: rounds to 4 decimals, derives grid from the balance equation so it holds exactly, derives charge/discharge/idle from the sign, recomputes totals from the returned plan, and writes a deterministic `plan_summary`.

`app/validator.py` is an independent judge-style replay checker (it doesn't import the optimizer) used by the tests and scripts.

## 3. Run it on a device (local)

Needs Python 3.11+ (3.12 tested) and git. On Windows, use WSL or Git Bash for the commands below.

```bash
cd gridwise
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

python -m pytest -q                      # expect: 25 passed  (no key needed)
python scripts/fuzz_optimizer.py 500     # expect: 0 failures

cp .env.example .env                     # put the real ANTHROPIC_API_KEY in .env
set -a; source .env; set +a

python scripts/eval_interpreter.py       # ← MOST IMPORTANT: live LLM accuracy, expect 72/72 (investigate every miss)
uvicorn app.main:app --host 0.0.0.0 --port 8000
# second terminal:
python scripts/run_public_samples.py http://localhost:8000     # expect 10/10 passed
```

**Getting a key:** create one at console.anthropic.com (API keys), add a small credit balance and set a spend limit. Any other provider works too: set `LLM_PROVIDER=openai`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `LLM_MODEL` (e.g. Groq, OpenRouter, Gemini's OpenAI-compatible endpoint).

**If `eval_interpreter.py` shows errors:** each line prints the note, what was extracted and why it's wrong. Fix by adding a matching example to `FEW_SHOT` or a rule line to `SYSTEM_PROMPT` in `app/prompt.py`, then re-run. Lines tagged `[backup]` mean the LLM call failed (check the key/model). Do not hard-code public note wording.

## 4. Ship it

1. **Repo:** create a **new private** GitHub repo (the rules require it be created after the question reveal and stay private until the deadline). Push this folder. Never commit `.env`.
2. **Image:** the push triggers `.github/workflows/docker.yml`: tests → build `linux/amd64` → push to `ghcr.io/<owner>/<repo>:sha-<commit>` and `:latest` → smoke-test the pushed image against the 10 samples. Check the Actions tab is green.
3. **Make the image pullable:** GitHub → your profile → Packages → the package → Package settings → change visibility to **Public**. (Packages from private repos start private.) Test from another machine: `docker pull ghcr.io/<owner>/<repo>:sha-<commit>`. Submit that exact tag (or the `@sha256:` digest shown on the package page).
   - Alternative: Docker Hub — `docker build -t <user>/gridwise:v1 . && docker push <user>/gridwise:v1`.
4. **Host:** any always-on container host. Render: New → Web Service → the repo → Docker runtime → **paid Starter instance** (free instances sleep after ~15 idle minutes and cold-start slowly) → add env var `ANTHROPIC_API_KEY` as a secret → health check path `/health`. `render.yaml` is included if you prefer a Blueprint. Railway, Fly.io (min 1 machine running) or a small VPS with `docker run` also work.
5. **Test from outside:** from a different network (phone hotspot is fine):
   `curl https://<host>/health`, `python scripts/run_public_samples.py https://<host> --repeat 3` → all pass, note the p95, then
   `python scripts/load_test.py https://<host> --n 60 --concurrency 10 --unique` → all 200 (this spends a little API credit).
6. **Fill placeholders** in README: `<REPO_URL>`, `ghcr.io/<OWNER>/<REPO>:<TAG>`, and confirm the model line in §2.
7. **Video:** record ≤ 3 min following `docs/VIDEO_SCRIPT.md`; upload somewhere judges can open.
8. **Submit:** base URL, repo URL, image reference with exact tag/digest + env-var names + port 8000 + the `docker run` command, video link. After the deadline, make the repo public. Keep the host, the image and the API key alive through the judging window, and keep enough API credit for repeated hidden tests.

Note on the Docker fallback: organizers running the image need their own `ANTHROPIC_API_KEY` for LLM interpretation. Without a key the service still answers correctly formed responses through the backup parser, which is better than failing, but the LLM path is what the rules require, so make sure the hosted URL (with your key) stays up.

## 5. Suggested 4-hour timeline (if doing it during the round)

| Time | Task |
|---|---|
| 0:00–0:20 | Create repo, push, start the Actions build, create the host service with the key |
| 0:20–1:00 | Local: tests, `eval_interpreter.py`, fix any prompt misses |
| 1:00–1:30 | External tests on the hosted URL; make GHCR package public; verify `docker pull` |
| 1:30–2:30 | Prompt hardening with extra paraphrases of your own (add to `tests/paraphrases.json`) |
| 2:30–3:15 | README placeholders, final external run, record video |
| 3:15–4:00 | Buffer; submit early |

## 6. Compliance notes

- The LLM is the primary interpreter; the backup parser runs only on LLM failure and says so in its explanations and in logs (`source=backup`). To be strict, you can set `ENABLE_RULE_FALLBACK=false`; failures then degrade to safe `no_op`.
- Credit AI assistance in the README (already in §8). The team should read through the code so you can explain it in the video and to judges.
- No secrets in the repo, image, logs or responses (verified: a test key never appeared in logs).

## 7. Files

| Path | What |
|---|---|
| `app/` | Service code (see README §7) |
| `tests/` | pytest suite, `paraphrases.json` (54 paraphrased notes with expected answers) |
| `scripts/run_public_samples.py` | Judge-style grader for any running URL, prints p50/p95 |
| `scripts/eval_interpreter.py` | Live LLM interpretation accuracy |
| `scripts/fuzz_optimizer.py` | 500-case optimizer fuzz vs independent LP |
| `scripts/load_test.py` | Concurrency / stability test for any running URL |
| `scripts/mock_llm_server.py` | Offline fake LLM (Anthropic + OpenAI formats) for demos/load tests without a key |
| `Dockerfile`, `.github/workflows/docker.yml`, `render.yaml`, `.env.example` | Deployment |
| `README.md` | Submission README |
| `PROGRESS.md` | Research, decisions, plan and build log |
| `docs/VIDEO_SCRIPT.md` | Video outline |
