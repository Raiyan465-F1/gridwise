# 3-minute video script (tie-break only)

Target: ≤ 2:50. Screen-record the README architecture diagram, the code, and a terminal. No editing needed.

**0:00–0:25 — Problem.** "GridWise gets a 24-hour campus profile — demand, solar, tariff — a battery, and 1–3 operator notes in plain English. We must understand the notes, turn them into one of six directive types, apply them, and return the cheapest schedule that obeys every energy rule."

**0:25–1:05 — Architecture (show README §3 diagram).** "Three stages. One: the LLM — we use Groq's openai/gpt-oss-120b, an OpenAI-compatible chat model — reads all notes in a single structured-output call and returns an intermediate form: type, start hour, exclusive end hour, value, and unit. Two: deterministic guardrails in `normalize.py` do the arithmetic the model shouldn't: build the hour list, wrap midnight, convert '80% reduction' to factor 0.2 and '50% of capacity' to kWh, and reject anything out of range or unsupported. Three: a linear program solved by HiGHS minimizes grid cost."

**1:05–1:40 — Key choices (show `optimizer.py` docstring, `interpreter.py`).** "No round-trip losses, so one net battery variable per hour is exact and forces one action per hour. Directives become bounds: solar factor scales the solar bound, reserves raise the energy floor, windows zero the charge or discharge bound, caps bound grid. Grid in the output is derived from the balance equation, and totals are recomputed from the plan, so the replay always matches. If the LLM returns 429 we honor the Retry-After header; if the output fails validation or makes the LP infeasible, we retry once with the validator's error; if the provider stays down, a clearly-labelled backup parser keeps the service answering — never an invented directive, never a crash."

**1:40–2:30 — Demo (terminal).** `curl /health` → ok. `curl` SAMPLE-06 → show the three interpretations incl. the no_op distractor. Run `python scripts/run_public_samples.py <URL>` → 10/10 pass, p95. Run `python scripts/eval_interpreter.py` → paraphrase accuracy, most lines tagged [llm].

**2:30–2:50 — Testing & run.** "An independent replay validator mirrors the judge; 500 random scenarios are checked against a second LP formulation with zero failures. Run it with `docker run -p 8000:8000 -e OPENAI_API_KEY=... -e LLM_PROVIDER=openai -e OPENAI_BASE_URL=https://api.groq.com/openai/v1 -e LLM_MODEL=openai/gpt-oss-120b <image>` or the README quickstart."
