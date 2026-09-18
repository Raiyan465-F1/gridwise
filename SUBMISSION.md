# GridWise — BUP CSE Fest 2026 submission

## Submission package

| Item | Value |
|---|---|
| Public service URL | _deployed during judging window — see below_ |
| Source repo (private until deadline) | https://github.com/Raiyan465-F1/gridwise |
| Docker image | `ghcr.io/raiyan465-f1/gridwise:sha-582b0cd` |
| Docker image (alias) | `ghcr.io/raiyan465-f1/gridwise:latest` |
| Port | 8000 |
| LLM provider | Groq `openai/gpt-oss-120b` (OpenAI-compatible, free tier) |

## How to run the image yourself

```bash
docker pull ghcr.io/raiyan465-f1/gridwise:sha-582b0cd
docker run --rm -p 8000:8000 \
  -e LLM_PROVIDER=openai \
  -e OPENAI_BASE_URL=https://api.groq.com/openai/v1 \
  -e OPENAI_API_KEY=<your-groq-key> \
  -e LLM_MODEL=openai/gpt-oss-120b \
  ghcr.io/raiyan465-f1/gridwise:sha-582b0cd
```

If you have your own Anthropic / OpenAI / OpenRouter key, replace the four env vars; the
service works against any OpenAI-compatible endpoint, or the Anthropic Messages API by
setting `LLM_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`.

Without an LLM key, the service still answers correctly via the rule-based backup
parser (every test case passes), but the LLM path is what the rules require for full marks.

## Endpoints

- `GET /health` → `200 {"status":"ok"}` within ~1 s of startup
- `POST /optimize-energy` → interpretation + 24-hour optimized plan in <30 s (p95 < 5 s)

## Quick self-test

```bash
curl -s http://localhost:8000/health
pip install httpx==0.28.1 numpy scipy
python scripts/run_public_samples.py http://localhost:8000
```

Expected: `10/10 passed | p50 ≈ 2 s | p95 ≈ 5 s`.

## What judges will verify

- **API contract** (`app/main.py`, `app/schemas.py`): exact endpoints, 400 / 422 on bad input, controlled 500s with no stack traces or secrets.
- **Optimization quality** (`app/optimizer.py`): scipy/HiGHS LP, reproduces the reference optimal cost on all 10 public samples and on a 500-case fuzz.
- **Directive application** (`app/optimizer.py` + `app/normalize.py`): every directive type becomes the right constraint. Independent replay validator (`app/validator.py`) checks all judge rules.
- **LLM interpretation** (`app/llm.py`, `app/prompt.py`, `app/normalize.py`): single structured-output call, intermediate form, deterministic arithmetic, no hard-coded public-sample phrasings.
- **Performance / reliability**: load-tested 100 × 20 concurrent. Provider 429s honor `Retry-After` so the service degrades gracefully (never an invented directive, never a crash).
- **Deployment**: standard `python:3.12-slim` image, binds 0.0.0.0:8000, no secrets baked in, non-root user.

## How to read this submission

- `README.md` — architecture, quickstart, rubric mapping
- `HANDOFF.md` — what's done, how to ship, 4-hour timeline
- `PROGRESS.md` — research log and decisions (read sections 9 for the live LLM story)
- `docs/VIDEO_SCRIPT.md` — 3-min video outline
- `app/` — service code (annotated)
- `tests/` — 25 pytest cases + 72-case paraphrase eval set
- `scripts/` — grader, fuzzer, load tester, mock LLM, live interpreter eval
