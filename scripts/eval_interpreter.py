"""Measure LLM interpretation accuracy on paraphrased notes (needs a real API key).

Calls the same interpretation pipeline the API uses (LLM -> guardrails), one note
at a time, against tests/paraphrases.json plus all public-sample notes.

Usage:
    export ANTHROPIC_API_KEY=...        # or LLM_PROVIDER=openai + OPENAI_* vars
    python scripts/eval_interpreter.py
    python scripts/eval_interpreter.py --no-backup   # fail loudly instead of using the backup parser
"""
import argparse
import asyncio
import json
import os
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def neutral_scenario(capacity):
    """Light-demand day where every valid directive is feasible, so only interpretation is measured."""
    return {"scenario_id": "EVAL", "operator_notes": [],
            "hours": [{"hour": h, "demand_kwh": 60, "solar_kwh": 50 if 7 <= h <= 17 else 0,
                       "tariff_bdt_per_kwh": 8} for h in range(24)],
            "battery": {"capacity_kwh": capacity, "initial_energy_kwh": capacity / 2,
                        "minimum_energy_kwh": 0, "max_charge_kwh_per_hour": capacity,
                        "max_discharge_kwh_per_hour": capacity}}


def load_items():
    items = [{**it, "scenario": neutral_scenario(it["capacity_kwh"])}
             for it in json.loads((ROOT / "tests" / "paraphrases.json").read_text())]
    for case in json.loads((ROOT / "data" / "public_sample_cases.json").read_text())["cases"]:
        for i, note in enumerate(case["input"]["operator_notes"]):
            items.append({"note": note, "scenario": case["input"],
                          "expected": case["expected_output"]["directive_interpretation"][i]})
    return items


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--delay", type=float, default=0.0,
                    help="Seconds to sleep between calls (helps with provider RPM limits).")
    args = ap.parse_args()
    if args.no_backup:
        os.environ["ENABLE_RULE_FALLBACK"] = "false"
    from app import interpreter
    from app.schemas import parse_scenario
    from app.validator import compare_interpretation

    ok = 0
    items = load_items()
    lat = []
    for it in items:
        req = {**it["scenario"], "operator_notes": [it["note"]]}
        sc = parse_scenario(req)
        interpreter._cache.data.clear()
        t0 = time.perf_counter()
        got, meta = await interpreter.interpret(sc)
        lat.append(time.perf_counter() - t0)
        errs = compare_interpretation([it["expected"]], got)
        ok += not errs
        flag = "ok " if not errs else "ERR"
        print(f"{flag} [{meta['source']:<10}] {lat[-1]*1000:5.0f}ms  {it['note'][:80]}")
        if errs:
            print("      ", errs, "|", got[0]["structured_adjustment"], meta["warnings"])
        if args.delay > 0:
            await asyncio.sleep(args.delay)
    lat.sort()
    print(f"\n{ok}/{len(items)} correct | p95 {lat[int(0.95*len(lat))-1]*1000:.0f} ms")


if __name__ == "__main__":
    asyncio.run(main())
