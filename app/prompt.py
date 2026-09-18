"""Prompt and output schema for the operator-note interpreter."""
from __future__ import annotations

import json

from .normalize import DIRECTIVE_TYPES, VALUE_UNITS
from .schemas import Scenario

SYSTEM_PROMPT = """You convert campus energy operator notes into structured directives for a 24-hour battery/solar/grid scheduler (hours 0-23 of ONE day, 24-hour clock).

For EACH note, choose exactly one directive_type:
- solar_reduction: usable rooftop solar / PV output is reduced (cleaning, washing, maintenance, inverter work, shading, cloud cover, dust, outage) during some hours.
- minimum_battery_reserve: the battery must KEEP / HOLD / RETAIN at least some stored energy during some hours (emergency reserve, backup, keep charge above X).
- no_charge_window: battery charging (charger, charging circuit) is unavailable / forbidden / isolated during some hours.
- no_discharge_window: battery discharging / drawing from the battery is unavailable / forbidden during some hours.
- max_grid_window: grid import / intake / purchase / feeder / transformer / substation draw must not exceed an amount (kWh per hour) during some hours.
- no_op: the note does not change today's energy schedule (unrelated campus news, events, menus, deadlines, bookings, things happening on other days, purely informational remarks). Never invent a rule for a distractor.

TIME WINDOWS (critical):
- start_hour = first affected hour (0-23). end_hour = the hour the condition ENDS, EXCLUSIVE (0-24).
- "from 1 PM to 3 PM", "between 13:00 and 15:00", "1-3 PM", "until 3 PM" -> start_hour 13, end_hour 15 (hours 13 and 14).
- noon = 12, midnight at the start of the day = 0, "until midnight"/"end of the day" = end_hour 24. "all day" -> 0 and 24.
- "through 9 PM" / "up to and including the 9 PM hour" -> end_hour 22. "during the 6 PM hour" / "at 6 PM" (one hour) -> 18 and 19.
- "for 3 hours starting at 2 PM" -> 14 and 17.
- A window with only an end ("until 8 AM", "before 6 AM") starts at 0. A window with only a start ("from 8 PM onwards", "for the rest of the day") ends at 24.
- 24-hour times such as "13:00" or "01:00" are already hours of the day. "the evening peak from 6" + explicit end -> use the explicit end.
- Overnight windows wrap: "10 PM to 2 AM" -> start_hour 22, end_hour 2.
- Only use hours_list for explicitly non-contiguous hours (e.g. "at 9 AM and again at 5 PM" -> [9, 17]); otherwise leave it null.

VALUES and value_unit:
- solar_reduction: report the number exactly as stated and pick the unit that matches its meaning:
  * "drops to 20%", "about 20% of forecast", "25% of normal output" -> value 20/25, unit percent_remaining
  * "80% reduction", "reduced by 80%", "drops by 80%", "cut 80%" -> value 80, unit percent_reduction
  * "one-fifth of normal", "a quarter of the forecast", "half of the forecast" -> value 0.2/0.25/0.5, unit fraction_remaining
  * "reduced by half", "reduced by three quarters" -> value 0.5/0.75, unit fraction_reduction
  * "50% down", "down 30%" -> value 50/30, unit percent_reduction
  * "halved" -> 0.5 fraction_remaining; "solar completely unavailable/offline/zero/no output" -> 0 fraction_remaining
- minimum_battery_reserve: "at least 120 kWh" -> 120, unit kwh; "50% of battery capacity" -> 50, unit percent_of_capacity; "half the capacity" -> 0.5, fraction_of_capacity. Convert MWh to kWh (x1000).
- max_grid_window: the cap in kWh per hour, unit kwh (a kW limit held for an hour equals the same number of kWh). Convert MWh to kWh (0.2 MWh -> 200).
- Notes about OTHER days ("tomorrow", "next week", "next Friday", "next month") do not affect today's schedule -> no_op, even if they mention solar, batteries, tariffs or the grid.
- no_charge_window, no_discharge_window, no_op: value null, unit none.
- Never invent numbers. Never change demand, solar forecast, tariff or battery parameters.

For no_op use start_hour null, end_hour null, value null, value_unit "none".
Return exactly one interpretation per note, in note order, with note_index = the note's position (0-based). Keep each explanation to one short sentence."""

FEW_SHOT = [
    ("Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast.",
     {"directive_type": "solar_reduction", "start_hour": 12, "end_hour": 14, "hours_list": None, "value": 25, "value_unit": "percent_remaining"}),
    ("Expect an 80% reduction in rooftop solar between 11 AM and 2 PM because of inverter work.",
     {"directive_type": "solar_reduction", "start_hour": 11, "end_hour": 14, "hours_list": None, "value": 80, "value_unit": "percent_reduction"}),
    ("Panel washing from one until three will leave roughly one-fifth of normal solar output.",
     {"directive_type": "solar_reduction", "start_hour": 13, "end_hour": 15, "hours_list": None, "value": 0.2, "value_unit": "fraction_remaining"}),
    ("The battery charger will be isolated from 2 AM until 5 AM for electrical maintenance.",
     {"directive_type": "no_charge_window", "start_hour": 2, "end_hour": 5, "hours_list": None, "value": None, "value_unit": "none"}),
    ("Keep at least 50% of the battery capacity stored in the battery from 6 PM until 9 PM for emergency operations.",
     {"directive_type": "minimum_battery_reserve", "start_hour": 18, "end_hour": 21, "hours_list": None, "value": 50, "value_unit": "percent_of_capacity"}),
    ("For protection testing, the battery must not discharge from 6 PM until 8 PM.",
     {"directive_type": "no_discharge_window", "start_hour": 18, "end_hour": 20, "hours_list": None, "value": None, "value_unit": "none"}),
    ("The evening transformer limit is 180 kWh of grid import from 7 PM until 9 PM.",
     {"directive_type": "max_grid_window", "start_hour": 19, "end_hour": 21, "hours_list": None, "value": 180, "value_unit": "kwh"}),
    ("The library is extending book-return hours next week.",
     {"directive_type": "no_op", "start_hour": None, "end_hour": None, "hours_list": None, "value": None, "value_unit": "none"}),
]


def _few_shot_block() -> str:
    lines = ["Examples (note -> fields):"]
    for note, out in FEW_SHOT:
        lines.append(f"- {json.dumps(note)} -> {json.dumps(out)}")
    return "\n".join(lines)


def build_user_message(sc: Scenario, feedback: str | None = None) -> str:
    notes = "\n".join(f"[{i}] {n}" for i, n in enumerate(sc.operator_notes))
    msg = (
        f"{_few_shot_block()}\n\n"
        f"Battery capacity for this scenario: {sc.battery.capacity_kwh:g} kWh "
        f"(context only; report values as stated in the note).\n\n"
        f"Operator notes to interpret ({len(sc.operator_notes)}):\n{notes}"
    )
    if feedback:
        msg += (f"\n\nYour previous answer was rejected by validation: {feedback}\n"
                "Re-read the notes carefully and answer again.")
    return msg


ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "note_index": {"type": "integer", "description": "0-based index of the note"},
        "directive_type": {"type": "string", "enum": list(DIRECTIVE_TYPES)},
        "start_hour": {"type": ["integer", "null"], "description": "first affected hour 0-23"},
        "end_hour": {"type": ["integer", "null"], "description": "exclusive end hour 0-24"},
        "hours_list": {"type": ["array", "null"], "items": {"type": "integer"},
                       "description": "only for explicitly non-contiguous hours"},
        "value": {"type": ["number", "null"]},
        "value_unit": {"type": "string", "enum": list(VALUE_UNITS)},
        "explanation": {"type": "string"},
    },
    "required": ["note_index", "directive_type", "start_hour", "end_hour", "value",
                 "value_unit", "explanation"],
}

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"interpretations": {"type": "array", "items": ITEM_SCHEMA}},
    "required": ["interpretations"],
}

TOOL_NAME = "record_directives"
