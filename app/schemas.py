"""Request validation for POST /optimize-energy.

Structural problems (missing fields, wrong types, wrong lengths, duplicate hours)
raise RequestError(400). Well-formed but semantically impossible inputs
(negative quantities, initial energy outside battery bounds, ...) raise
RequestError(422), as allowed by Problem Statement §6.1.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

BATTERY_FIELDS = (
    "capacity_kwh",
    "initial_energy_kwh",
    "minimum_energy_kwh",
    "max_charge_kwh_per_hour",
    "max_discharge_kwh_per_hour",
)
HOUR_FIELDS = ("demand_kwh", "solar_kwh", "tariff_bdt_per_kwh")


class RequestError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class Battery:
    capacity_kwh: float
    initial_energy_kwh: float
    minimum_energy_kwh: float
    max_charge_kwh_per_hour: float
    max_discharge_kwh_per_hour: float


@dataclass
class Scenario:
    scenario_id: str
    operator_notes: list[str]
    demand: list[float]   # index = hour 0..23
    solar: list[float]
    tariff: list[float]
    battery: Battery


def _num(value: Any, where: str) -> float:
    # bool is a subclass of int in Python; reject it explicitly.
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RequestError(400, f"{where} must be a number")
    if not math.isfinite(value):
        raise RequestError(400, f"{where} must be finite")
    return float(value)


def parse_scenario(body: Any) -> Scenario:
    if not isinstance(body, dict):
        raise RequestError(400, "request body must be a JSON object")

    for key in ("scenario_id", "operator_notes", "hours", "battery"):
        if key not in body:
            raise RequestError(400, f"missing required field '{key}'")

    sid = body["scenario_id"]
    if not isinstance(sid, str) or not sid.strip():
        raise RequestError(400, "scenario_id must be a non-empty string")

    notes = body["operator_notes"]
    if not isinstance(notes, list) or not (1 <= len(notes) <= 3):
        raise RequestError(400, "operator_notes must be an array of 1-3 strings")
    for i, n in enumerate(notes):
        if not isinstance(n, str) or not n.strip():
            raise RequestError(400, f"operator_notes[{i}] must be a non-empty string")

    hours = body["hours"]
    if not isinstance(hours, list) or len(hours) != 24:
        raise RequestError(400, "hours must be an array of exactly 24 entries")
    by_hour: dict[int, dict] = {}
    for i, h in enumerate(hours):
        if not isinstance(h, dict):
            raise RequestError(400, f"hours[{i}] must be an object")
        for key in ("hour",) + HOUR_FIELDS:
            if key not in h:
                raise RequestError(400, f"hours[{i}] missing '{key}'")
        hr = h["hour"]
        if isinstance(hr, bool) or not isinstance(hr, (int, float)) or float(hr) != int(hr):
            raise RequestError(400, f"hours[{i}].hour must be an integer")
        hr = int(hr)
        if not 0 <= hr <= 23:
            raise RequestError(400, f"hours[{i}].hour must be between 0 and 23")
        if hr in by_hour:
            raise RequestError(400, f"duplicate hour {hr}")
        by_hour[hr] = h

    demand, solar, tariff = [], [], []
    for hr in range(24):
        h = by_hour[hr]
        d = _num(h["demand_kwh"], f"hour {hr} demand_kwh")
        s = _num(h["solar_kwh"], f"hour {hr} solar_kwh")
        t = _num(h["tariff_bdt_per_kwh"], f"hour {hr} tariff_bdt_per_kwh")
        if d < 0 or s < 0 or t < 0:
            raise RequestError(422, f"hour {hr}: demand, solar and tariff must be non-negative")
        demand.append(d)
        solar.append(s)
        tariff.append(t)

    b = body["battery"]
    if not isinstance(b, dict):
        raise RequestError(400, "battery must be an object")
    vals = {}
    for key in BATTERY_FIELDS:
        if key not in b:
            raise RequestError(400, f"battery missing '{key}'")
        vals[key] = _num(b[key], f"battery.{key}")
        if vals[key] < 0:
            raise RequestError(422, f"battery.{key} must be non-negative")
    battery = Battery(**vals)
    if battery.minimum_energy_kwh > battery.capacity_kwh:
        raise RequestError(422, "battery.minimum_energy_kwh exceeds capacity_kwh")
    if not battery.minimum_energy_kwh <= battery.initial_energy_kwh <= battery.capacity_kwh:
        raise RequestError(422, "battery.initial_energy_kwh must be between minimum_energy_kwh and capacity_kwh")

    return Scenario(
        scenario_id=sid,
        operator_notes=[n.strip() for n in notes],
        demand=demand,
        solar=solar,
        tariff=tariff,
        battery=battery,
    )
