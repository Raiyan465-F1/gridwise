# GridWise — LLM-Assisted Campus Energy Optimizer

BUP CSE Fest 2026 Hackathon · Online Preliminary · Smart Campus Energy Optimization Challenge

One HTTP service that reads 1–3 natural-language operator notes, turns them into validated structured directives with an LLM, applies them to a linear-programming model, and returns a minimum-cost, rule-valid 24-hour grid / solar / battery schedule.

| Endpoint | Purpose |
|---|---|
| `GET /health` | Readiness → `{"status":"ok"}` |
| `POST /optimize-energy` | Scenario in → `directive_interpretation` + `hourly_plan` + totals out |

---

## 1. Quickstart (clean machine, ~3 minutes)

Requirements: Python 3.11+ (tested on 3.12) and `git`. Docker optional.

```bash
git clone https://github.com/Raiyan465-F1/gridwise.git gridwise && cd gridwise
python3 -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env            # then put your key in .env (never commit it)
set -a; source .env; set +a                              # load .env into this shell (or export the variables manually)

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

In a second terminal:

```bash
curl -s http://localhost:8000/health
# {"status":"ok"}

python - <<'EOF' > /tmp/sample.json
import json; print(json.dumps(json.load(open("data/public_sample_cases.json"))["cases"][5]["input"]))
EOF
curl -s -X POST http://localhost:8000/optimize-energy \
     -H "Content-Type: application/json" --data @/tmp/sample.json

python scripts/run_public_samples.py http://localhost:8000
# expected: "10/10 passed | p50 ... | p95 ..."   (exit code 0)
```

## 2. Configuration (environment variable names)

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `LLM_PROVIDER` | no | `anthropic` | `anthropic`, or `openai` for any OpenAI-compatible Chat Completions API |
| `LLM_MODEL` | no | `claude-haiku-4-5-20251001` | Model id for the chosen provider |
| `ANTHROPIC_API_KEY` | if provider = anthropic | — | Anthropic API key |
| `OPENAI_API_KEY` | if provider = openai | — | Key for the OpenAI-compatible provider |
| `OPENAI_BASE_URL` | no | `https://api.openai.com/v1` | e.g. Groq / OpenRouter / Together / local Ollama `/v1` endpoint |
| `PORT` | no | `8000` | Listen port (binds `0.0.0.0`) |
| `LLM_TIMEOUT_SECONDS` | no | `8` | Per LLM call timeout |
| `LLM_MAX_ATTEMPTS` | no | `2` | LLM attempts before the backup path |
| `ENABLE_RULE_FALLBACK` | no | `true` | Use the rule-based backup parser only if the LLM fails |
| `INTERPRETATION_CACHE_SIZE` | no | `512` | LRU cache of validated LLM interpretations |
| `WEB_WORKERS` | no | `2` | Uvicorn workers (Docker image) |
| `LOG_LEVEL` | no | `INFO` | Logging level |

**Model / provider used for submission:** Groq `openai/gpt-oss-120b` via Groq's OpenAI-compatible Chat Completions endpoint (`https://api.groq.com/openai/v1`). Groq's free tier supplies 1000 requests/day and 8000 tokens/minute per key, which the service honors via `Retry-After` / `x-ratelimit-reset-*` headers on 429 responses.

## 3. Architecture

```
request JSON
   │  schemas.py        strict request validation (400 structural / 422 semantic)
   ▼
operator_notes ──► LLM (llm.py + prompt.py)         forced tool call → intermediate JSON per note
                     │  {directive_type, start_hour, end_hour(excl.), value, value_unit, ...}
                     ▼
               normalize.py  DETERMINISTIC GUARDRAILS
                     │  allowed types · one entry per note, in order · hours built in code
                     │  (start-incl/end-excl, midnight wrap, unique, ascending, 0–23)
                     │  unit conversion (80% reduction→0.2, % of capacity→kWh) · range checks
                     │  no_op ⇔ applies=false + null
                     ▼
               feasibility check (LP) ── invalid/infeasible → retry LLM once with validator feedback
                     │                  ── provider down / still invalid → backup parser (same guardrails)
                     │                  ── still unusable for a note → that note becomes a safe no_op
                     ▼
               optimizer.py  LINEAR PROGRAM (scipy.optimize.linprog, HiGHS)
                     ▼
               output cleaning: grid derived from the balance equation, action from sign,
               totals recomputed from the returned plan
                     ▼
response JSON (directive_interpretation, hourly_plan, totals, plan_summary)
```

### LLM role
The LLM is the primary interpreter of every operator note. It decides relevance (distractor → `no_op`), the directive type, the time window and the numeric value *as stated*, with its unit. It does **not** do arithmetic that is easy to get subtly wrong (exclusive end hours, midnight wrap, "80% reduction" → factor 0.2, "50% of capacity" → kWh): code does that. The model is called once per request for all notes (temperature 0, forced tool call so the output is JSON matching a schema), with few-shot examples of every directive type and of distractors.

### Guardrails (PS §8)
LLM output is untrusted until it passes `app/normalize.py`: supported types only; each `note_index` exactly once, returned in order; hours unique ints 0–23 ascending; `0 ≤ factor ≤ 1`; reserve finite, ≥ 0, ≤ capacity; grid cap finite, ≥ 0; `no_op` ⇔ `applies=false` + `null`; exact `structured_adjustment` shape. Demand, solar forecast, tariff and battery parameters are never modified. After optimization the returned plan is built so every directive holds by construction, and `app/validator.py` independently replays plans in tests.

**Safe failure:** malformed/unsupported LLM output never crashes the service or invents a directive: it triggers one retry with the validation error as feedback, then the backup parser, then a per-note safe `no_op`. Non-retryable provider errors (401/403/404/400) skip the retry. If directives make the schedule infeasible, the offending note is first re-read by the backup parser, and only dropped to `no_op` if that fails. LLM calls time out after 8 s (2 attempts, 20 s total budget), keeping every response under the 30 s judge timeout.

### Optimizer
Variables per hour: grid `g ≥ 0`, solar used `0 ≤ s ≤ effective_solar`, charge `c` and discharge `d` (bounded by rate limits); net flow `n = c − d`. Constraints: `g + s − n = demand`; `max(min_energy, reserve directive) ≤ E₀ + Σn ≤ capacity` every hour; `Σn = 0` (end-of-day neutrality); grid caps; `n ≤ 0` in no-charge hours, `n ≥ 0` in no-discharge hours. Objective: minimize `Σ tariff·g` plus a negligible `1e-5 × (c + d)` tie-breaker so equally cheap plans avoid pointless charge/discharge cycling. There are no round-trip losses in the challenge, so the returned plan uses the net flow, which is exact and guarantees a single action per hour. HiGHS solves it in a few milliseconds and returns the global optimum.

## 4. Testing

```bash
pip install -r requirements-dev.txt
python -m pytest -q                         # unit + API tests (fake LLM; no key needed)
python scripts/fuzz_optimizer.py 500        # random scenarios vs an independent LP formulation
python scripts/run_public_samples.py URL    # grade a running service on the 10 public cases
python scripts/eval_interpreter.py          # LIVE LLM accuracy on 54 paraphrases + 18 sample notes (needs key)
python scripts/load_test.py URL --n 100 --concurrency 20 --unique   # concurrency / stability
```

No key? `scripts/mock_llm_server.py` imitates the Anthropic and OpenAI APIs (with optional latency and failure rate) so the full HTTP path can be exercised offline:

```bash
MOCK_LLM_DELAY=1.5 MOCK_LLM_FAIL_RATE=0.1 uvicorn scripts.mock_llm_server:app --port 9000 &
ANTHROPIC_API_KEY=dummy ANTHROPIC_BASE_URL=http://127.0.0.1:9000 uvicorn app.main:app --port 8000
```

Verified results (this repository, before deployment):

| Check | Result |
|---|---|
| Public samples, optimizer with reference directives | 10/10 valid, cost equal to reference optimum |
| Fuzz: 500 random scenarios/directives | 468 feasible, 0 validity failures, max cost gap to independent optimum 0.00005 BDT |
| `pytest` | 25 passed (API, guardrails, optimizer, edge cases: zero-size battery, zero tariff, shuffled/float hours, cache, retries) |
| Public samples over HTTP (backup parser path, no key) | 10/10 pass, p95 ≈ 10 ms |
| Provider failure (invalid key → HTTP 401) | 10/10 pass via backup, no retry wasted, no key in logs |
| Load: 100 requests, 20 concurrent, mock LLM 1.5 s ± 40 % with 10 % failures | 100 × HTTP 200, 0 invalid plans, p95 3.5 s |
| Hung provider (never answers) | every request answers in ~16 s (< 30 s judge timeout) via backup |

## 5. Docker (fallback execution path)

The image contains no secrets; pass the key at run time.

```bash
docker pull ghcr.io/raiyan465-f1/gridwise:sha-1efbf2d
docker run --rm -p 8000:8000 \
  -e LLM_PROVIDER=openai \
  -e OPENAI_BASE_URL=https://api.groq.com/openai/v1 \
  -e OPENAI_API_KEY=<your-groq-key> \
  -e LLM_MODEL=openai/gpt-oss-120b \
  ghcr.io/raiyan465-f1/gridwise:sha-1efbf2d
curl -s http://localhost:8000/health
python scripts/run_public_samples.py http://localhost:8000
```

Build locally instead: `docker build -t gridwise . && docker run --rm -p 8000:8000 -e ANTHROPIC_API_KEY=<key> gridwise`.
Exposed port: `8000` (override with `-e PORT=...`). The GitHub Actions workflow `.github/workflows/docker.yml` runs the tests, builds `linux/amd64`, pushes `ghcr.io/<owner>/<repo>:sha-<commit>` and `:latest`, and smoke-tests the pushed image.

## 6. Sample request / response

Request: any `cases[i].input` object from `data/public_sample_cases.json`, e.g. SAMPLE-06 (three notes: solar reduction, charging outage, library distractor).

Response (hourly plan truncated):

```json
{
  "scenario_id": "SAMPLE-06",
  "directive_interpretation": [
    {"note_index": 0, "applies": true, "directive_type": "solar_reduction",
     "structured_adjustment": {"hours": [10, 11], "factor": 0.5}, "explanation": "..."},
    {"note_index": 1, "applies": true, "directive_type": "no_charge_window",
     "structured_adjustment": {"hours": [14, 15]}, "explanation": "..."},
    {"note_index": 2, "applies": false, "directive_type": "no_op",
     "structured_adjustment": null, "explanation": "..."}
  ],
  "hourly_plan": [
    {"hour": 0, "grid_kwh": 105.0, "solar_used_kwh": 0.0, "battery_action": "charge",
     "battery_kwh": 20.0, "battery_energy_after_kwh": 120.0},
    "... 22 more hours ...",
    {"hour": 23, "grid_kwh": 160.0, "solar_used_kwh": 0.0, "battery_action": "charge",
     "battery_kwh": 50.0, "battery_energy_after_kwh": 100.0}
  ],
  "total_grid_kwh": 2395.0,
  "total_cost_bdt": 34090.0,
  "peak_grid_kwh": 175.0,
  "plan_summary": "Minimum-cost 24-hour plan via linear programming: ..."
}
```

Error responses are JSON `{"error": "..."}` with status 400 (malformed JSON / structurally invalid), 422 (well-formed but impossible values, e.g. initial energy above capacity) or 500 (controlled; no stack traces).

## 7. Project layout

```
app/
  main.py            FastAPI routes, error handling, plan_summary
  schemas.py         request validation
  prompt.py          system prompt, few-shot examples, output JSON schema
  llm.py             Anthropic + OpenAI-compatible clients (httpx)
  normalize.py       deterministic guardrails: intermediate → structured directives
  interpreter.py     LLM → guardrails → retry → backup → safe no_op; LRU cache
  fallback_parser.py rule-based BACKUP parser (only when the LLM fails)
  optimizer.py       LP model + output cleaning
  validator.py       independent judge-style replay validator (used by tests/scripts)
data/public_sample_cases.json
scripts/  run_public_samples.py · eval_interpreter.py · fuzz_optimizer.py · load_test.py · mock_llm_server.py
tests/    pytest suite + paraphrases.json
Dockerfile · .github/workflows/docker.yml · render.yaml · .env.example
```

## 8. Dependencies & credits

Python 3.12, FastAPI, Uvicorn, httpx, Pydantic, NumPy, SciPy (HiGHS LP solver, via `scipy.optimize.linprog`), pytest. LLM: Anthropic Claude API (Claude Haiku 4.5). AI coding assistance: Claude (Anthropic) was used while developing this repository; architecture and code were reviewed by the team.

## 9. Secret handling

Keys are read only from environment variables. `.env` is git-ignored and docker-ignored; nothing secret is baked into the image. Logs record provider status codes and error class names only — never keys, prompts with secrets, raw provider bodies or stack traces. API error responses contain a short message only.

## 10. Known limitations

- Interpretation quality depends on the LLM. The prompt covers the published paraphrase styles, but unusual phrasings can still be misread; run `scripts/eval_interpreter.py` after any prompt/model change.
- Ambiguous time words ("from one until three" with no AM/PM) are resolved to the daytime reading.
- Overlapping directives of the same type are combined conservatively (lowest solar factor, highest reserve, lowest grid cap).
- If every LLM attempt fails, the rule-based backup parser is used; it handles common phrasings only. Set `ENABLE_RULE_FALLBACK=false` to disable it.
- When several schedules have the same optimal cost, the LP may return any of them (the judge accepts equivalent optimal schedules).
- Free hosting tiers that sleep when idle (e.g. Render free: ~15 min) cause slow first requests; use an always-on instance for judging.
