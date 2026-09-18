"""Run the public sample cases against a running service and grade them like the judge.

Usage:
    python scripts/run_public_samples.py                       # http://localhost:8000
    python scripts/run_public_samples.py https://your-app.example.com
    python scripts/run_public_samples.py URL --repeat 3        # latency / stability check

For every case it checks: HTTP 200, response schema, interpretation vs the public
reference (type/applies/hours/values), replay validity against the reference
directives, and cost vs the reference optimal cost. Prints p50/p95 latency.
Exit code 0 only if every case passes.
"""
import argparse
import json
import pathlib
import statistics
import sys
import time

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from app.validator import check_interpretation_schema, compare_interpretation, replay  # noqa: E402

DATA = pathlib.Path(__file__).resolve().parents[1] / "data" / "public_sample_cases.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url", nargs="?", default="http://localhost:8000")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--file", default=str(DATA))
    args = ap.parse_args()
    base = args.base_url.rstrip("/")
    cases = json.loads(pathlib.Path(args.file).read_text())["cases"]

    with httpx.Client(timeout=35) as c:
        h = c.get(f"{base}/health")
        print(f"/health -> {h.status_code} {h.text}")
        lat, failures = [], 0
        for rep in range(args.repeat):
            for case in cases:
                inp, exp = case["input"], case["expected_output"]
                gt = exp["directive_interpretation"]
                t0 = time.perf_counter()
                r = c.post(f"{base}/optimize-energy", json=inp)
                dt = time.perf_counter() - t0
                lat.append(dt)
                problems = []
                if r.status_code != 200:
                    problems.append(f"HTTP {r.status_code}: {r.text[:200]}")
                else:
                    body = r.json()
                    problems += check_interpretation_schema(inp, body)
                    problems += compare_interpretation(gt, body.get("directive_interpretation", []))
                    problems += replay(inp, gt, body)
                    cost = body.get("total_cost_bdt")
                    if isinstance(cost, (int, float)) and cost > exp["total_cost_bdt"] + 0.01:
                        problems.append(f"cost {cost} > optimal {exp['total_cost_bdt']}")
                status = "PASS" if not problems else "FAIL"
                failures += bool(problems)
                print(f"[{rep}] {case['id']:<10} {status} {dt*1000:7.0f} ms  {'; '.join(problems[:4])}")
    lat.sort()
    p95 = lat[max(0, int(round(0.95 * len(lat))) - 1)]
    print(f"\n{len(lat) - failures}/{len(lat)} passed | p50 {statistics.median(lat)*1000:.0f} ms | "
          f"p95 {p95*1000:.0f} ms | max {lat[-1]*1000:.0f} ms")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
