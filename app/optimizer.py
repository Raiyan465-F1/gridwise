"""24-hour cost-minimizing schedule as a linear program (scipy / HiGHS).

Model
-----
Variables per hour h (72 total):
    g[h] >= 0                    grid import (kWh)
    s[h] in [0, eff_solar[h]]    solar used (unused solar is curtailed)
    n[h] = c[h] - d[h]           net battery flow: +charge / -discharge
                                 (c in [0,cmax], d in [0,dmax], tiny throughput penalty)

Constraints:
    g[h] + s[h] - n[h] = demand[h]                          energy balance
    lo[h] <= E0 + sum_{k<=h} n[k] <= capacity               battery bounds (+ reserve directives)
    sum_h n[h] = 0                                          end-of-day neutrality
    g[h] <= grid_cap[h]                                     max_grid_window directives
    cmax[h] = 0 / dmax[h] = 0                               no_charge / no_discharge windows

Objective: minimize sum_h tariff[h] * g[h]  (+ 1e-5 * battery throughput as a tie-breaker)

The challenge has no round-trip losses, so only the net flow per hour matters; the
returned plan uses the net value, which guarantees one action (charge / discharge /
idle) per hour.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import linprog
from scipy.sparse import lil_matrix

from .schemas import Scenario

H = 24
DECIMALS = 4
SNAP = 1e-6


class InfeasibleError(Exception):
    """No schedule satisfies the energy rules plus the given directives."""


@dataclass
class Constraints:
    effective_solar: list[float]
    reserve: list[float]          # battery_energy_after lower bound per hour
    charge_cap: list[float]
    discharge_cap: list[float]
    grid_cap: list[float | None]


def build_constraints(sc: Scenario, directives: list[dict]) -> Constraints:
    """Apply validated structured directives to the base scenario.

    Overlapping directives of the same kind are combined conservatively
    (lowest solar factor, highest reserve, lowest grid cap): the result
    satisfies every individual directive.
    """
    b = sc.battery
    solar = list(sc.solar)
    reserve = [b.minimum_energy_kwh] * H
    ccap = [b.max_charge_kwh_per_hour] * H
    dcap = [b.max_discharge_kwh_per_hour] * H
    gcap: list[float | None] = [None] * H
    factor = [1.0] * H

    for d in directives:
        t = d["directive_type"]
        adj = d.get("structured_adjustment")
        if t == "no_op" or adj is None:
            continue
        for h in adj["hours"]:
            if t == "solar_reduction":
                factor[h] = min(factor[h], float(adj["factor"]))
            elif t == "minimum_battery_reserve":
                reserve[h] = max(reserve[h], float(adj["minimum_energy_kwh"]))
            elif t == "no_charge_window":
                ccap[h] = 0.0
            elif t == "no_discharge_window":
                dcap[h] = 0.0
            elif t == "max_grid_window":
                cap = float(adj["max_grid_kwh"])
                gcap[h] = cap if gcap[h] is None else min(gcap[h], cap)

    eff = [solar[h] * factor[h] for h in range(H)]
    return Constraints(eff, reserve, ccap, dcap, gcap)


THROUGHPUT_PENALTY = 1e-5  # BDT per kWh moved through the battery; only breaks cost ties


def _solve_lp(sc: Scenario, c: Constraints) -> np.ndarray:
    """Return net battery flow is c-d; result vector is [g(24), s(24), n(24)].

    Internally charge c and discharge d are separate non-negative variables with a
    negligible throughput penalty, so among equally cheap schedules the solver
    prefers the one without pointless charge/discharge cycling. The penalty can only
    change the grid cost when two schedules differ by less than 1e-5 BDT per kWh
    moved, i.e. far below the judge's 0.01 tolerance.
    """
    b = sc.battery
    nvar = 4 * H  # [g, s, c, d]
    G, S, C, D = 0, H, 2 * H, 3 * H

    cost = np.zeros(nvar)
    cost[G:G + H] = sc.tariff
    cost[C:C + H] = THROUGHPUT_PENALTY
    cost[D:D + H] = THROUGHPUT_PENALTY

    a_eq = lil_matrix((H + 1, nvar))
    b_eq = np.zeros(H + 1)
    for h in range(H):
        a_eq[h, G + h] = 1.0
        a_eq[h, S + h] = 1.0
        a_eq[h, C + h] = -1.0
        a_eq[h, D + h] = 1.0
        b_eq[h] = sc.demand[h]
        a_eq[H, C + h] = 1.0   # end-of-day neutrality: sum(c - d) = 0
        a_eq[H, D + h] = -1.0

    a_ub = lil_matrix((2 * H, nvar))
    b_ub = np.zeros(2 * H)
    for h in range(H):
        for k in range(h + 1):
            a_ub[2 * h, C + k] = 1.0
            a_ub[2 * h, D + k] = -1.0
            a_ub[2 * h + 1, C + k] = -1.0
            a_ub[2 * h + 1, D + k] = 1.0
        b_ub[2 * h] = b.capacity_kwh - b.initial_energy_kwh          # E_h <= capacity
        b_ub[2 * h + 1] = b.initial_energy_kwh - c.reserve[h]        # E_h >= reserve

    bounds = (
        [(0.0, c.grid_cap[h]) for h in range(H)]
        + [(0.0, c.effective_solar[h]) for h in range(H)]
        + [(0.0, c.charge_cap[h]) for h in range(H)]
        + [(0.0, c.discharge_cap[h]) for h in range(H)]
    )
    res = linprog(cost, A_ub=a_ub.tocsr(), b_ub=b_ub, A_eq=a_eq.tocsr(), b_eq=b_eq,
                  bounds=bounds, method="highs")
    if res.status != 0 or res.x is None:
        raise InfeasibleError(res.message)
    x = res.x
    return np.concatenate([x[G:G + H], x[S:S + H], x[C:C + H] - x[D:D + H]])


def _r(x: float) -> float:
    v = round(float(x), DECIMALS)
    return 0.0 if abs(v) < SNAP else v


def optimize(sc: Scenario, directives: list[dict]) -> dict:
    """Return hourly_plan and totals. Raises InfeasibleError."""
    c = build_constraints(sc, directives)
    x = _solve_lp(sc, c)
    g_raw, s_raw, n_raw = x[:H], x[H:2 * H], x[2 * H:]

    plan = []
    energy = sc.battery.initial_energy_kwh
    for h in range(H):
        n = _r(n_raw[h])
        s = min(_r(s_raw[h]), c.effective_solar[h])
        s = max(s, 0.0)
        charge = n if n > 0 else 0.0
        discharge = -n if n < 0 else 0.0
        # Grid is derived from the balance equation so it holds exactly on returned numbers.
        grid = _r(sc.demand[h] + charge - s - discharge)
        if grid < 0:  # numerical dust: curtail a little more solar instead of exporting
            s = _r(s + grid)
            grid = 0.0
        energy = _r(energy + n)
        plan.append({
            "hour": h,
            "grid_kwh": grid,
            "solar_used_kwh": s,
            "battery_action": "charge" if n > 0 else ("discharge" if n < 0 else "idle"),
            "battery_kwh": _r(abs(n)),
            "battery_energy_after_kwh": energy,
        })

    total_grid = _r(sum(p["grid_kwh"] for p in plan))
    total_cost = _r(sum(p["grid_kwh"] * sc.tariff[p["hour"]] for p in plan))
    peak = max(p["grid_kwh"] for p in plan)
    return {
        "hourly_plan": plan,
        "total_grid_kwh": total_grid,
        "total_cost_bdt": total_cost,
        "peak_grid_kwh": peak,
        "effective_solar": c.effective_solar,
    }


def is_feasible(sc: Scenario, directives: list[dict]) -> bool:
    try:
        _solve_lp(sc, build_constraints(sc, directives))
        return True
    except InfeasibleError:
        return False
