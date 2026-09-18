# PROGRESS.md — build log

Running log of what was researched, decided, built and verified. Newest entries at the bottom of each section.

## 1. Requirements digest (from the 3 organizer documents)

- **Service:** one public HTTP API. `GET /health` → `200 {"status":"ok"}` within 60 s of start. `POST /optimize-energy` → interpretation + 24-h plan, must finish < 30 s (p95 ≤ 5 s for full latency points).
- **LLM is mandatory** in the operator-note interpretation path. Hard-coded phrase matching as the *sole* interpreter is non-compliant.
- **Directive types:** `solar_reduction {hours, factor}`, `minimum_battery_reserve {hours, minimum_energy_kwh}`, `no_charge_window {hours}`, `no_discharge_window {hours}`, `max_grid_window {hours, max_grid_kwh}`, `no_op null`.
- **Rules:** one entry per note in order; `no_op` ⇔ `applies=false` + `null`; hours unique ints 0–23 ascending; windows are start-inclusive / end-exclusive; solar `factor` = usable fraction remaining (80% reduction → 0.2).
- **Energy rules:** `grid + solar_used + discharge = demand + charge`; `min ≤ E ≤ capacity`; hourly charge/discharge limits; `solar_used ≤ effective_solar`; no export; final E = initial E; one action per hour (charge / discharge / idle).
- **Objective:** minimize Σ grid·tariff. Score = min(1, optimal/team_cost). Invalid plan = 0.
- **Errors:** 400 malformed / structurally invalid; 422 optional semantic; 500 controlled, no stack traces or secrets.
- **Deliverables:** public URL, private-then-public GitHub repo (created after question reveal), excellent README, pullable Docker image (exact tag/digest, binds 0.0.0.0, no secrets), ≤ 3-min video (tie-break only).
- **Scoring (100):** interpretation 25 · directive application 25 · optimization 10 · API/schema 10 · performance/reliability 10 · deployment/Docker 10 · docs/reproducibility 10.

## 2. Research & evaluation

| Question | Finding | Decision |
|---|---|---|
| Best optimizer? | Problem is a pure LP: no charge/discharge efficiency losses, so a single *net* battery variable per hour is exact and makes "one action per hour" automatic. | scipy `linprog` (HiGHS). Verified: reproduces the reference optimal cost on **10/10** public samples. Solves in ms. |
| How to get reliable JSON from the LLM? | Claude API has no `response_format`; the documented structured-output path is a forced tool call (`tool_choice: {"type":"tool", ...}`). | Forced tool call for Anthropic; `response_format: json_object` for OpenAI-compatible providers. Output is still treated as untrusted and validated in code. |
| Let the LLM compute hour arrays and factors directly? | Most likely error sources: off-by-one on exclusive end hour, midnight wrap, "80% reduction" vs "80% remaining", "% of capacity" reserves. These are deterministic arithmetic. | LLM extracts an **intermediate** form (`start_hour`, `end_hour`, `value`, `value_unit`); code does the arithmetic (range building, wrap, unit conversion). Semantics stay with the LLM, math stays in code. |
| Which model? | Latency drives 3 pts; accuracy drives 25+. Small fast models handle this extraction well with few-shot examples. | Default `claude-haiku-4-5-20251001`, configurable via `LLM_MODEL`. Any OpenAI-compatible provider also supported (`LLM_PROVIDER=openai`). |
| What if the LLM is down? | Rubric awards points for controlled provider-failure handling; judge cases keep coming. | Retry once, then a clearly-labelled rule-based **backup** parser (only when the LLM fails; can be disabled with `ENABLE_RULE_FALLBACK=false`). |
| Hosting | Render free web services spin down after 15 minutes idle; cold start can take ~1 minute. | Use a paid/always-on instance (Render Starter, Railway, Fly with min 1 machine, a VPS) during judging. |
| Puku | Coding assistant (IDE/CLI), not a runtime model API. | Optional dev tool only; not used by the service. |

## 3. Plan

1. Project skeleton, config via env vars. ✅
2. Request validation (400/422) matching the exact schema. ✅
3. LP optimizer + output cleaning (rounding, energy-balance-exact grid, action derivation, totals from plan). ✅
4. Independent replay validator (every judge check in PS §11.3 + directive checks). ✅
5. Interpretation: prompt + few-shot, provider clients, intermediate→structured normalizer, guardrails, retry, feasibility check, backup parser. ✅
6. FastAPI app: routes, error handlers, caching, deterministic `plan_summary`. ✅
7. Tests: unit + API (with fake LLM) + public samples; paraphrase set for live LLM eval. ✅
8. Dockerfile, `.dockerignore`, GitHub Actions → GHCR, `.env.example`. ✅
9. README (all rubric-required sections), HANDOFF.md, video script. ✅
10. Needs a real device / API key: live LLM eval, docker build/push, GitHub repo, hosting, video. ⏳ (steps in HANDOFF.md)

## 4. Build log

(entries appended as work progresses)
- 15:25 UTC: skeleton, config, request validation, LP optimizer, replay validator, normalizer/guardrails, prompt, LLM clients, interpreter, backup parser, FastAPI app written.
- Optimizer: 10/10 public samples at reference optimal cost; 500-case fuzz vs independent LP formulation: 468 feasible, 0 failures (validity + cost).
- Backup parser: 36/36 on paraphrase set, 10/10 public samples (backup only; LLM is primary).
- pytest: 16 passed (optimizer, guardrails, API incl. fake-LLM end-to-end, provider-down, retry, garbage output, 400/422).
- HTTP runs in sandbox: server started with uvicorn; `run_public_samples.py` 20/20 pass (backup path, no key), p95 ≈ 10 ms; malformed JSON → 400, missing field → 400.
- Real Anthropic endpoint reached with an invalid test key → HTTP 401 handled, backup used, 10/10 pass, key never appears in logs.
- Mock LLM server (Anthropic tool_use format and OpenAI chat format, incl. ```json fences): both provider code paths 10/10 pass with `source=llm`; cache hit on repeats.
- Clean venv install from pinned `requirements-dev.txt`: 16 tests pass. README quickstart executed literally in a fresh copy: works.
- `eval_interpreter.py` harness fixed to use a neutral feasible scenario for paraphrases (earlier version conflated scenario infeasibility with misinterpretation). Backup parser: 54/54 there.
- Dockerfile, GitHub Actions (test → build amd64 → push GHCR → smoke test), render.yaml, .env.example, README, HANDOFF.md, docs/VIDEO_SCRIPT.md written.

## 5. Not done / could not be done here

- **Live LLM accuracy** — no API key in the sandbox. Prompt, schema and request format are built per Anthropic's documented forced-tool-call pattern and verified against a mock, but the real model's accuracy and latency must be measured with `scripts/eval_interpreter.py`.
- **Docker build/push** — no Docker in the sandbox. The Dockerfile is standard (python:3.12-slim); CI builds and smoke-tests it.
- **GitHub repo, hosting, video** — need the team's accounts and a device.

## 6. Ideas if time remains

- Expand `tests/paraphrases.json` with the team's own tricky phrasings (overnight windows, "through", MWh units) and iterate on the prompt.
- Run 2 LLM calls in parallel and take the one that validates first to cut tail latency (costs 2× tokens).

## 7. Round 2 — hardening plan

1. Don't retry non-retryable provider errors (401/403/404/400); go straight to backup to save latency.
2. Run CPU-bound LP work in a thread pool so concurrent requests don't block the event loop.
3. Tidier schedules: split charge/discharge with a negligible throughput penalty (no pointless charge→discharge cycling), re-verify cost optimality.
4. Edge-case tests: zero-size battery, zero tariff, shuffled hours, extra fields, float hour values.
5. Harder paraphrase set (overnight, "through", MWh, 24-h clock, mixed notes).
6. Concurrency/load test against the running server.
7. Offline mock LLM server script so the pipeline can be demoed without a key.
8. Validate the YAML files (CI workflow, render.yaml).

### Round 2 log

- Non-retryable provider errors (400/401/403/404/422, missing key) now skip the retry and go straight to backup. Test added.
- LP work moved off the event loop (`run_in_threadpool` / `asyncio.to_thread`).
- Optimizer now uses separate charge/discharge variables with a 1e-5 BDT/kWh throughput tie-breaker (no pointless cycling). Fuzz 500: 0 failures; max cost gap to independent optimum 0.00005 BDT (rounding). Fuzz tolerance tightened to 0.01.
- Infeasible LLM directive → the note is re-read by the backup parser before being dropped to no_op. Test added.
- 9 edge-case tests added (zero-size battery, zero tariff + zero solar, shuffled + float hours + extra fields, fractional values, exact scenario_id echo, cache hit, auth error not retried, infeasible directive repair, 405/404). pytest: 25 passed.
- Paraphrase set 36 → 54 (overnight wraps, 24-h clock, MWh, fractions, open-ended windows, single hour, other-day distractors mentioning solar/battery/tariff). Prompt extended for: open-ended windows, fraction_reduction ("reduced by three quarters"), "50% down", MWh example, other-day notes → no_op. Backup parser fixed (fraction words / durations were parsed as clock times); 72/72 on the eval set.
- LLM timeout default 12 s → 8 s, total budget 20 s. Hung-provider test: every request answers in ~16 s with valid plans.
- `scripts/mock_llm_server.py` (fake Anthropic/OpenAI with latency + failure injection) and `scripts/load_test.py` added. Load: 100 requests, 20 concurrent, 1.5 s ± 40 % latency, 10 % failures → 100 × 200, 0 invalid, p95 3.5 s; the 10 injected failures were all recovered by retry.
- CI workflow and render.yaml parse as valid YAML.
- Mistake noted: my process-kill command matched its own shell and killed it twice; switched to killing by process name only.

## 8. Local device run (2026-09-18)

- venv via `uv` on Python 3.12.13 (host default is 3.14; 3.12 matches the Dockerfile). Removed empty stray `{app,tests,...}` dirs left by a brace-expansion typo.
- pytest: 25 passed. Fuzz 500: 468 feasible, 0 failures.
- Local server, no key (backup path): public samples 10/10, p95 43 ms; malformed JSON → 400.
- Mock LLM (port 9100; 9000 is taken on this host), 1.5 s delay, 10 % failures: samples 10/10 (p95 3.8 s); load 100 × 20 concurrent → 100 × 200, 0 invalid, p95 3.3 s; 108 LLM / 2 backup interpretations.
- **Docker verified:** `docker build -t gridwise:local .` OK (494 MB); container smoke test 10/10, p95 37 ms, 400 on malformed body.
- Still pending: real API key → `eval_interpreter.py`; GitHub repo + GHCR push; hosting; video.
