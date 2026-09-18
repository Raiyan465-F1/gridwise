"""Fuzz test: random scenarios + random directives.

For every feasible random case, checks that
  1. the returned plan passes the independent replay validator, and
  2. its cost equals an independent LP formulation (separate charge/discharge vars).
Usage: python scripts/fuzz_optimizer.py [n_cases]
"""
import random
import sys

import numpy as np
from scipy.optimize import linprog

sys.path.insert(0, ".")
from app.optimizer import InfeasibleError, optimize  # noqa: E402
from app.schemas import parse_scenario  # noqa: E402
from app.validator import replay  # noqa: E402


def rand_case(rng):
    cap = rng.choice([150, 200, 250, 300, 500])
    mn = rng.uniform(0, 0.3) * cap
    init = rng.uniform(mn, cap)
    hours = []
    for h in range(24):
        sun = max(0.0, np.sin((h - 6) / 12 * np.pi)) if 6 <= h <= 18 else 0.0
        hours.append({"hour": h, "demand_kwh": round(rng.uniform(60, 260), rng.choice([0, 2])),
                      "solar_kwh": round(sun * rng.uniform(50, 250), rng.choice([0, 2])),
                      "tariff_bdt_per_kwh": round(rng.uniform(3, 15), rng.choice([0, 2]))})
    bat = {"capacity_kwh": cap, "initial_energy_kwh": round(init, 2), "minimum_energy_kwh": round(mn, 2),
           "max_charge_kwh_per_hour": rng.choice([30, 50, 80, 120]),
           "max_discharge_kwh_per_hour": rng.choice([30, 50, 80, 120])}
    dirs = []
    for _ in range(rng.randint(0, 3)):
        s = rng.randint(0, 22)
        hrs = list(range(s, min(24, s + rng.randint(1, 5))))
        t = rng.choice(["solar_reduction", "minimum_battery_reserve", "no_charge_window",
                        "no_discharge_window", "max_grid_window", "no_op"])
        adj = {"hours": hrs}
        if t == "solar_reduction":
            adj["factor"] = round(rng.uniform(0, 1), 2)
        elif t == "minimum_battery_reserve":
            adj["minimum_energy_kwh"] = round(rng.uniform(0, cap), 1)
        elif t == "max_grid_window":
            adj["max_grid_kwh"] = round(rng.uniform(80, 300), 1)
        dirs.append({"directive_type": t, "applies": t != "no_op",
                     "structured_adjustment": None if t == "no_op" else adj})
    return {"scenario_id": "FUZZ", "operator_notes": ["x"] * max(1, len(dirs)),
            "hours": hours, "battery": bat}, dirs


def independent_cost(req, dirs):
    H = 24
    b = req["battery"]
    dem = [h["demand_kwh"] for h in req["hours"]]
    sol = [h["solar_kwh"] for h in req["hours"]]
    tar = [h["tariff_bdt_per_kwh"] for h in req["hours"]]
    lo = [b["minimum_energy_kwh"]] * H
    cc = [b["max_charge_kwh_per_hour"]] * H
    dc = [b["max_discharge_kwh_per_hour"]] * H
    gc = [None] * H
    for d in dirs:
        a = d["structured_adjustment"]
        if not a:
            continue
        for h in a["hours"]:
            t = d["directive_type"]
            if t == "solar_reduction":
                sol[h] = min(sol[h], req["hours"][h]["solar_kwh"] * a["factor"])
            elif t == "minimum_battery_reserve":
                lo[h] = max(lo[h], a["minimum_energy_kwh"])
            elif t == "no_charge_window":
                cc[h] = 0
            elif t == "no_discharge_window":
                dc[h] = 0
            else:
                gc[h] = a["max_grid_kwh"] if gc[h] is None else min(gc[h], a["max_grid_kwh"])
    # vars: g, s, c, d  (4*24)
    n = 4 * H
    cost = np.r_[tar, np.zeros(3 * H)]
    Aeq, beq = [], []
    for h in range(H):
        r = np.zeros(n); r[h] = 1; r[H + h] = 1; r[2 * H + h] = -1; r[3 * H + h] = 1
        Aeq.append(r); beq.append(dem[h])
    r = np.zeros(n); r[2 * H:3 * H] = 1; r[3 * H:] = -1; Aeq.append(r); beq.append(0)
    Aub, bub = [], []
    for h in range(H):
        r = np.zeros(n); r[2 * H:2 * H + h + 1] = 1; r[3 * H:3 * H + h + 1] = -1
        Aub.append(r); bub.append(b["capacity_kwh"] - b["initial_energy_kwh"])
        Aub.append(-r); bub.append(b["initial_energy_kwh"] - lo[h])
    bounds = [(0, gc[h]) for h in range(H)] + [(0, sol[h]) for h in range(H)] + \
             [(0, cc[h]) for h in range(H)] + [(0, dc[h]) for h in range(H)]
    res = linprog(cost, A_ub=Aub, b_ub=bub, A_eq=Aeq, b_eq=beq, bounds=bounds, method="highs")
    return res.fun if res.status == 0 else None


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300
    rng = random.Random(42)
    feasible = bad = 0
    for _ in range(n):
        req, dirs = rand_case(rng)
        ref = independent_cost(req, dirs)
        try:
            plan = optimize(parse_scenario(req), dirs)
        except InfeasibleError:
            assert ref is None, "optimizer says infeasible but reference solved"
            continue
        feasible += 1
        resp = {"scenario_id": "FUZZ", "directive_interpretation": [], "plan_summary": "",
                **{k: plan[k] for k in ("hourly_plan", "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh")}}
        errs = replay(req, dirs, resp)
        if errs or abs(plan["total_cost_bdt"] - ref) > 0.01:
            bad += 1
            print("FAIL", errs[:3], plan["total_cost_bdt"], ref)
    print(f"{n} random cases, {feasible} feasible, {bad} failures")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
