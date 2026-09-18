"""Independent replay validator that mirrors the judge checks (PS §9, §11).

Deliberately does NOT import the optimizer: it re-derives everything from the raw
request, a list of directives (e.g. organizer ground truth), and the response.
Returns a list of human-readable violations (empty list == valid).
"""
from __future__ import annotations

import math
from typing import Any

TOL = 0.01
TYPES = {"solar_reduction", "minimum_battery_reserve", "no_charge_window",
         "no_discharge_window", "max_grid_window", "no_op"}
SHAPES = {
    "solar_reduction": {"hours", "factor"},
    "minimum_battery_reserve": {"hours", "minimum_energy_kwh"},
    "no_charge_window": {"hours"},
    "no_discharge_window": {"hours"},
    "max_grid_window": {"hours", "max_grid_kwh"},
}
PLAN_FIELDS = {"hour", "grid_kwh", "solar_used_kwh", "battery_action", "battery_kwh",
               "battery_energy_after_kwh"}
TOP_FIELDS = {"scenario_id", "directive_interpretation", "hourly_plan", "total_grid_kwh",
              "total_cost_bdt", "peak_grid_kwh", "plan_summary"}


def _finite(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def check_interpretation_schema(req: dict, resp: dict) -> list[str]:
    errs = []
    di = resp.get("directive_interpretation")
    notes = req["operator_notes"]
    if not isinstance(di, list) or len(di) != len(notes):
        return [f"directive_interpretation must have {len(notes)} entries"]
    for i, e in enumerate(di):
        p = f"interp[{i}]"
        if e.get("note_index") != i:
            errs.append(f"{p}: note_index should be {i}")
        t = e.get("directive_type")
        if t not in TYPES:
            errs.append(f"{p}: unsupported type {t!r}")
            continue
        if not isinstance(e.get("explanation"), str):
            errs.append(f"{p}: explanation must be a string")
        adj = e.get("structured_adjustment")
        if t == "no_op":
            if e.get("applies") is not False or adj is not None:
                errs.append(f"{p}: no_op needs applies=false and null adjustment")
            continue
        if e.get("applies") is not True:
            errs.append(f"{p}: non-no_op needs applies=true")
        if not isinstance(adj, dict) or set(adj) != SHAPES[t]:
            errs.append(f"{p}: adjustment keys must be {sorted(SHAPES[t])}")
            continue
        hrs = adj["hours"]
        if (not isinstance(hrs, list) or not hrs or any(isinstance(h, bool) or not isinstance(h, int) for h in hrs)
                or hrs != sorted(set(hrs)) or hrs[0] < 0 or hrs[-1] > 23):
            errs.append(f"{p}: hours must be unique ascending ints 0-23")
        for k in SHAPES[t] - {"hours"}:
            if not _finite(adj[k]) or adj[k] < 0:
                errs.append(f"{p}: {k} must be finite and non-negative")
        if t == "solar_reduction" and _finite(adj["factor"]) and adj["factor"] > 1:
            errs.append(f"{p}: factor must be <= 1")
        if t == "minimum_battery_reserve" and _finite(adj["minimum_energy_kwh"]) \
                and adj["minimum_energy_kwh"] > req["battery"]["capacity_kwh"] + TOL:
            errs.append(f"{p}: reserve exceeds capacity")
    return errs


def compare_interpretation(expected: list[dict], got: list[dict]) -> list[str]:
    """Compare against organizer ground truth (type, applies, hours, numeric values)."""
    errs = []
    for i, (e, g) in enumerate(zip(expected, got)):
        if e["directive_type"] != g.get("directive_type") or e["applies"] != g.get("applies"):
            errs.append(f"note {i}: expected {e['directive_type']}, got {g.get('directive_type')}")
            continue
        ea, ga = e["structured_adjustment"], g.get("structured_adjustment")
        if ea is None:
            continue
        if ga is None:
            errs.append(f"note {i}: missing adjustment")
            continue
        if ea["hours"] != ga.get("hours"):
            errs.append(f"note {i}: hours {ga.get('hours')} != {ea['hours']}")
        for k, v in ea.items():
            if k != "hours" and (not _finite(ga.get(k)) or abs(ga[k] - v) > TOL):
                errs.append(f"note {i}: {k} {ga.get(k)} != {v}")
    return errs


def replay(req: dict, directives: list[dict], resp: dict) -> list[str]:
    """Replay the schedule against energy rules + the given (ground-truth) directives."""
    errs = []
    missing = TOP_FIELDS - set(resp)
    if missing:
        errs.append(f"missing top-level fields {sorted(missing)}")
    if resp.get("scenario_id") != req["scenario_id"]:
        errs.append("scenario_id not echoed")
    plan = resp.get("hourly_plan")
    if not isinstance(plan, list) or len(plan) != 24:
        return errs + ["hourly_plan must have 24 entries"]
    if sorted(p.get("hour") for p in plan) != list(range(24)):
        return errs + ["hourly_plan hours must be 0..23 unique"]
    plan = sorted(plan, key=lambda p: p["hour"])

    hours = {h["hour"]: h for h in req["hours"]}
    b = req["battery"]
    solar = {h: hours[h]["solar_kwh"] for h in range(24)}
    reserve = {h: b["minimum_energy_kwh"] for h in range(24)}
    no_c, no_d, gcap = set(), set(), {}
    for d in directives:
        adj = d.get("structured_adjustment")
        if d["directive_type"] == "no_op" or adj is None:
            continue
        for h in adj["hours"]:
            t = d["directive_type"]
            if t == "solar_reduction":
                solar[h] = min(solar[h], hours[h]["solar_kwh"] * adj["factor"])
            elif t == "minimum_battery_reserve":
                reserve[h] = max(reserve[h], adj["minimum_energy_kwh"])
            elif t == "no_charge_window":
                no_c.add(h)
            elif t == "no_discharge_window":
                no_d.add(h)
            elif t == "max_grid_window":
                gcap[h] = min(gcap.get(h, math.inf), adj["max_grid_kwh"])

    e_prev = b["initial_energy_kwh"]
    for p in plan:
        h = p["hour"]
        if set(p) < PLAN_FIELDS:
            errs.append(f"h{h}: missing fields {sorted(PLAN_FIELDS - set(p))}")
            continue
        g, s, a, k, e = (p["grid_kwh"], p["solar_used_kwh"], p["battery_action"],
                         p["battery_kwh"], p["battery_energy_after_kwh"])
        if not all(_finite(v) and v >= -TOL for v in (g, s, k, e)):
            errs.append(f"h{h}: non-finite or negative values")
            continue
        if a not in ("charge", "discharge", "idle"):
            errs.append(f"h{h}: bad action {a!r}")
            continue
        if a == "idle" and abs(k) > TOL:
            errs.append(f"h{h}: idle with battery_kwh {k}")
        charge = k if a == "charge" else 0.0
        dis = k if a == "discharge" else 0.0
        if charge > b["max_charge_kwh_per_hour"] + TOL:
            errs.append(f"h{h}: charge rate exceeded")
        if dis > b["max_discharge_kwh_per_hour"] + TOL:
            errs.append(f"h{h}: discharge rate exceeded")
        if s > solar[h] + TOL:
            errs.append(f"h{h}: solar used {s} > effective {solar[h]}")
        if abs(g + s + dis - hours[h]["demand_kwh"] - charge) > TOL:
            errs.append(f"h{h}: energy balance off")
        expect_e = e_prev + charge - dis
        if abs(e - expect_e) > TOL:
            errs.append(f"h{h}: battery transition {e_prev}->{e} with {a} {k}")
        if e < reserve[h] - TOL or e > b["capacity_kwh"] + TOL:
            errs.append(f"h{h}: energy {e} outside [{reserve[h]}, {b['capacity_kwh']}]")
        if h in no_c and charge > TOL:
            errs.append(f"h{h}: charged during no_charge_window")
        if h in no_d and dis > TOL:
            errs.append(f"h{h}: discharged during no_discharge_window")
        if h in gcap and g > gcap[h] + TOL:
            errs.append(f"h{h}: grid {g} > cap {gcap[h]}")
        e_prev = e
    if abs(e_prev - b["initial_energy_kwh"]) > TOL:
        errs.append(f"final energy {e_prev} != initial {b['initial_energy_kwh']}")

    tg = sum(p["grid_kwh"] for p in plan)
    tc = sum(p["grid_kwh"] * hours[p["hour"]]["tariff_bdt_per_kwh"] for p in plan)
    pk = max(p["grid_kwh"] for p in plan)
    if not _finite(resp.get("total_grid_kwh")) or abs(resp["total_grid_kwh"] - tg) > TOL:
        errs.append("total_grid_kwh mismatch")
    if not _finite(resp.get("total_cost_bdt")) or abs(resp["total_cost_bdt"] - tc) > TOL:
        errs.append("total_cost_bdt mismatch")
    if not _finite(resp.get("peak_grid_kwh")) or abs(resp["peak_grid_kwh"] - pk) > TOL:
        errs.append("peak_grid_kwh mismatch")
    return errs
