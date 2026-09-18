"""Concurrency / stability test against a running service.

Fires N requests (public sample inputs, optionally with perturbed notes so the cache
is bypassed) with C concurrent clients; reports status codes, validity and latency.

    python scripts/load_test.py http://localhost:8000 --n 100 --concurrency 10 --unique
"""
import argparse
import asyncio
import json
import pathlib
import sys
import time

import httpx

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.validator import check_interpretation_schema, replay  # noqa: E402


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base_url", nargs="?", default="http://localhost:8000")
    ap.add_argument("--n", type=int, default=100)
    ap.add_argument("--concurrency", type=int, default=10)
    ap.add_argument("--unique", action="store_true", help="append a suffix to notes to defeat the cache")
    a = ap.parse_args()
    cases = json.loads((ROOT / "data" / "public_sample_cases.json").read_text())["cases"]
    sem = asyncio.Semaphore(a.concurrency)
    results = []

    async def one(i, client):
        case = cases[i % len(cases)]
        inp = json.loads(json.dumps(case["input"]))
        if a.unique:
            inp["operator_notes"] = [n + f" (ref {i})" for n in inp["operator_notes"]]
        async with sem:
            t0 = time.perf_counter()
            try:
                r = await client.post(f"{a.base_url.rstrip('/')}/optimize-energy", json=inp)
                dt = time.perf_counter() - t0
                ok = r.status_code == 200
                errs = []
                if ok:
                    body = r.json()
                    errs = check_interpretation_schema(inp, body) + replay(
                        inp, case["expected_output"]["directive_interpretation"], body)
                results.append((r.status_code, dt, errs))
            except httpx.HTTPError as exc:
                results.append((type(exc).__name__, time.perf_counter() - t0, ["transport error"]))

    t0 = time.perf_counter()
    async with httpx.AsyncClient(timeout=35) as client:
        await asyncio.gather(*(one(i, client) for i in range(a.n)))
    wall = time.perf_counter() - t0
    lat = sorted(r[1] for r in results)
    codes = {}
    for r in results:
        codes[r[0]] = codes.get(r[0], 0) + 1
    invalid = sum(1 for r in results if r[2])
    p = lambda q: lat[min(len(lat) - 1, int(q * len(lat)))] * 1000
    print(f"{a.n} requests, concurrency {a.concurrency}, wall {wall:.1f}s")
    print(f"status codes: {codes} | plans failing replay vs reference: {invalid}")
    print(f"latency ms: p50 {p(0.5):.0f} | p95 {p(0.95):.0f} | max {lat[-1]*1000:.0f}")
    sys.exit(1 if invalid or set(codes) != {200} else 0)


if __name__ == "__main__":
    asyncio.run(main())
