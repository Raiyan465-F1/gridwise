"""Deterministic guardrails: LLM intermediate output -> validated structured directives.

The LLM is asked for an *intermediate* representation per note:

    {note_index, directive_type, start_hour, end_hour, hours_list,
     value, value_unit, explanation}

This module does all arithmetic deterministically:
  * builds the hours array (start inclusive, end exclusive, wraps past midnight,
    unique, ascending, 0-23),
  * converts value units (percent reduction -> remaining factor, percent of
    capacity -> kWh, ...),
  * enforces every guardrail in Problem Statement §8.

Anything that cannot be made valid raises InterpretationError; the caller retries
the LLM or falls back. Nothing unsupported is ever passed to the optimizer.
"""
from __future__ import annotations

import math
from typing import Any

from .schemas import Scenario

DIRECTIVE_TYPES = (
    "solar_reduction",
    "minimum_battery_reserve",
    "no_charge_window",
    "no_discharge_window",
    "max_grid_window",
    "no_op",
)
VALUE_UNITS = (
    "kwh",
    "percent_of_capacity",
    "fraction_of_capacity",
    "fraction_remaining",
    "percent_remaining",
    "percent_reduction",
    "fraction_reduction",
    "none",
)
NEEDS_VALUE = {"solar_reduction", "minimum_battery_reserve", "max_grid_window"}


class InterpretationError(Exception):
    pass


def _to_number(x: Any, what: str) -> float:
    if isinstance(x, bool):
        raise InterpretationError(f"{what} must be a number")
    if isinstance(x, str):
        try:
            x = float(x.strip().rstrip("%"))
        except ValueError as exc:
            raise InterpretationError(f"{what} must be a number") from exc
    if not isinstance(x, (int, float)) or not math.isfinite(x):
        raise InterpretationError(f"{what} must be a finite number")
    return float(x)


def _to_int(x: Any, what: str) -> int:
    v = _to_number(x, what)
    if v != int(v):
        raise InterpretationError(f"{what} must be a whole hour")
    return int(v)


def build_hours(start: Any, end: Any, hours_list: Any) -> list[int]:
    if isinstance(hours_list, list) and hours_list:
        hrs = {_to_int(h, "hours_list item") for h in hours_list}
        if any(h < 0 or h > 23 for h in hrs):
            raise InterpretationError("hours_list values must be 0-23")
        return sorted(hrs)
    if start is None or end is None:
        raise InterpretationError("time window missing (start_hour/end_hour)")
    s, e = _to_int(start, "start_hour"), _to_int(end, "end_hour")
    if s == 24:
        s = 0
    if not (0 <= s <= 23 and 0 <= e <= 24):
        raise InterpretationError("start_hour must be 0-23 and end_hour 0-24")
    if e == s:
        return [s]  # degenerate window: the single named hour
    if e > s:
        return list(range(s, e))
    return sorted(set(range(s, 24)) | set(range(0, e)))  # wraps past midnight


def _solar_factor(value: float, unit: str) -> float:
    if unit == "fraction_remaining":
        f = value
    elif unit == "percent_remaining":
        f = value / 100.0
    elif unit == "percent_reduction":
        f = 1.0 - value / 100.0
    elif unit == "fraction_reduction":
        f = 1.0 - value
    else:
        raise InterpretationError(f"unit {unit!r} not valid for solar_reduction")
    f = round(f, 6)
    if not 0.0 <= f <= 1.0:
        raise InterpretationError(f"solar factor {f} outside [0, 1]")
    return f


def _reserve_kwh(value: float, unit: str, capacity: float) -> float:
    if unit == "kwh":
        v = value
    elif unit == "percent_of_capacity":
        v = capacity * value / 100.0
    elif unit == "fraction_of_capacity":
        v = capacity * value
    else:
        raise InterpretationError(f"unit {unit!r} not valid for minimum_battery_reserve")
    v = round(v, 6)
    if v < 0 or v > capacity + 1e-9:
        raise InterpretationError(f"reserve {v} kWh outside [0, capacity]")
    return v


def normalize_one(item: dict, sc: Scenario) -> dict:
    t = item.get("directive_type")
    if t not in DIRECTIVE_TYPES:
        raise InterpretationError(f"unsupported directive_type {t!r}")
    idx = item["note_index"]
    expl = item.get("explanation")
    expl = expl.strip()[:300] if isinstance(expl, str) and expl.strip() else ""

    if t == "no_op":
        return {
            "note_index": idx,
            "applies": False,
            "directive_type": "no_op",
            "structured_adjustment": None,
            "explanation": expl or "This note does not affect the current 24-hour energy schedule.",
        }

    hours = build_hours(item.get("start_hour"), item.get("end_hour"), item.get("hours_list"))
    adj: dict[str, Any] = {"hours": hours}

    if t in NEEDS_VALUE:
        if item.get("value") is None:
            raise InterpretationError(f"{t} requires a numeric value")
        value = _to_number(item["value"], "value")
        unit = str(item.get("value_unit") or "").strip().lower()
        if unit not in VALUE_UNITS:
            raise InterpretationError(f"unknown value_unit {unit!r}")
        if t == "solar_reduction":
            adj["factor"] = _solar_factor(value, unit)
        elif t == "minimum_battery_reserve":
            adj["minimum_energy_kwh"] = _reserve_kwh(value, unit, sc.battery.capacity_kwh)
        else:  # max_grid_window
            if unit not in ("kwh", "none"):
                raise InterpretationError(f"unit {unit!r} not valid for max_grid_window")
            if value < 0:
                raise InterpretationError("max_grid_kwh must be non-negative")
            adj["max_grid_kwh"] = round(value, 6)

    return {
        "note_index": idx,
        "applies": True,
        "directive_type": t,
        "structured_adjustment": adj,
        "explanation": expl or f"Interpreted as {t}.",
    }


def normalize_all(raw: Any, sc: Scenario) -> list[dict]:
    """Validate the full LLM payload: exactly one entry per note, in order."""
    if isinstance(raw, dict):
        raw = raw.get("interpretations", raw.get("directives"))
    if not isinstance(raw, list):
        raise InterpretationError("expected a list of interpretations")
    n = len(sc.operator_notes)
    by_idx: dict[int, dict] = {}
    for pos, item in enumerate(raw):
        if not isinstance(item, dict):
            raise InterpretationError("each interpretation must be an object")
        idx = item.get("note_index", pos)
        idx = _to_int(idx, "note_index")
        if not 0 <= idx < n:
            raise InterpretationError(f"note_index {idx} does not match any note")
        if idx in by_idx:
            raise InterpretationError(f"duplicate note_index {idx}")
        by_idx[idx] = {**item, "note_index": idx}
    if len(by_idx) != n:
        missing = sorted(set(range(n)) - set(by_idx))
        raise InterpretationError(f"missing interpretation for notes {missing}")
    return [normalize_one(by_idx[i], sc) for i in range(n)]
