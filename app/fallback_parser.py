"""Rule-based BACKUP interpreter.

Used only when the LLM is unavailable or its output fails validation after all
retries (and ENABLE_RULE_FALLBACK=true). The LLM remains the primary interpreter,
as the challenge requires; this exists so a provider outage degrades gracefully
instead of failing the request. It emits the same intermediate format as the LLM,
so its output passes through the same deterministic guardrails.
"""
from __future__ import annotations

import re

WORD_NUM = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
FRACTIONS = {
    "one-fifth": 0.2, "one fifth": 0.2, "a fifth": 0.2, "one-quarter": 0.25, "a quarter": 0.25,
    "one quarter": 0.25, "one-third": 1 / 3, "a third": 1 / 3, "half": 0.5, "halved": 0.5,
    "three-quarters": 0.75, "three quarters": 0.75, "two-thirds": 2 / 3,
}
ENERGY_WORDS = r"solar|pv|photovoltaic|panel|array|battery|batteries|charg|grid|import|intake|feeder|transformer|substation|reserve"
NEGATION = r"not|no |nobody|no one|unavailable|disabled|isolated|offline|prohibited|forbidden|blocked|suspended|paused|cannot|can't|must not|out of service|locked out|down"

TIME_RE = re.compile(
    r"\b(noon|midday|midnight|(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?|"
    r"(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)(?:\s*(a\.?m\.?|p\.?m\.?|o'clock))?)(?![\d%])",
    re.IGNORECASE,
)


def _strip_quantities(text: str) -> str:
    # fractions ("three-quarters") and durations ("one hour") are not clock times
    text = re.sub(r"\b(?:one|two|three|a)[\s-](?:quarters?|fifths?|thirds?|half)\b", " ", text, flags=re.IGNORECASE)
    text = re.sub(r"\b(?:\d+|one|two|three|four|five|six)\s+hours?\b", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\d+(?:\.\d+)?\s*(?:%|percent\b|kwh\b|kw\b|mwh\b|mw\b|bdt\b|taka\b)", " ", text, flags=re.IGNORECASE)


def _times(text: str) -> list[tuple[int, str | None, bool]]:
    """Return [(hour, meridiem, is_24h_style)] in order of appearance."""
    out = []
    for m in TIME_RE.finditer(_strip_quantities(text)):
        tok = m.group(1).lower()
        if tok in ("noon", "midday"):
            out.append((12, "pm", True))
            continue
        if tok == "midnight":
            out.append((0, "am", True))
            continue
        if m.group(2):
            h = int(m.group(2))
            mer = (m.group(4) or "").replace(".", "").lower() or None
            is24 = bool(m.group(3)) and mer is None
            if h > 24:
                continue
            out.append((h, mer, is24 or h > 12))
        elif m.group(5):
            mer = (m.group(6) or "").replace(".", "").lower()
            out.append((WORD_NUM[m.group(5).lower()], mer if mer in ("am", "pm") else None, False))
    return out


def _to24(h: int, mer: str | None) -> int:
    if mer == "am":
        return 0 if h == 12 else h
    if mer == "pm":
        return h if h == 12 else h + 12
    return h


def _window(text: str) -> tuple[int | None, int | None]:
    ts = _times(text)
    if not ts:
        return None, None
    if len(ts) == 1:
        h, mer, is24 = ts[0]
        h = h if is24 else _to24(h, mer or ("pm" if 1 <= h <= 6 else None))
        low = text.lower()
        if re.search(r"\b(until|till|before|up to)\b", low) and not re.search(r"\bfrom\b", low):
            return 0, (24 if h == 0 else h)
        if re.search(r"onwards|rest of the day|end of the day|remainder of the day", low):
            return h, 24
        return h, h + 1
    (h1, m1, a24), (h2, m2, b24) = ts[0], ts[1]
    if not a24 and not b24:
        if m1 is None and m2 is not None:
            m1 = m2 if h1 <= h2 or h1 == 12 else ("am" if m2 == "pm" else "pm")
            if h1 == 12:
                m1 = "pm"
        if m2 is None and m1 is not None:
            m2 = m1 if h2 > h1 else ("pm" if m1 == "am" else "am")
        if m1 is None and m2 is None:  # bare words: assume the daytime reading
            m1 = "pm" if 1 <= h1 <= 6 or h1 == 12 else "am"
            m2 = "pm" if 1 <= h2 <= 7 or h2 == 12 else "am"
    s = h1 if a24 else _to24(h1, m1)
    e = h2 if b24 else _to24(h2, m2)
    if e == 0 and s > 0:
        e = 24
    return s, e


def _percent(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)", text, re.IGNORECASE)
    return float(m.group(1)) if m else None


def _kwh(text: str) -> float | None:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(mwh|kwh|kw|mw)\b", text, re.IGNORECASE)
    if not m:
        return None
    v = float(m.group(1))
    return v * 1000 if m.group(2).lower() in ("mwh", "mw") else v


def interpret_note(note: str) -> dict:
    t = note.lower()
    base = {"hours_list": None, "explanation": "Interpreted by the rule-based backup parser."}
    noop = {**base, "directive_type": "no_op", "start_hour": None, "end_hour": None,
            "value": None, "value_unit": "none"}
    if not re.search(ENERGY_WORDS, t):
        return noop
    start, end = _window(note)

    if re.search(r"discharg|draw(ing)? (energy |power )?from the battery", t) and re.search(NEGATION, t):
        kind = "no_discharge_window"
    elif re.search(r"(?<!dis)charg", t) and re.search(NEGATION, t) and "reserve" not in t:
        kind = "no_charge_window"
    elif re.search(r"reserve", t) or (re.search(r"keep|retain|hold|maintain|at least|minimum|remain|below|drop", t) and "batter" in t):
        kind = "minimum_battery_reserve"
    elif re.search(r"grid|import|intake|feeder|transformer|substation", t) and (_kwh(note) is not None):
        kind = "max_grid_window"
    elif re.search(r"solar|pv|photovoltaic|panel|array", t):
        kind = "solar_reduction"
    else:
        return noop
    if start is None:
        return noop

    out = {**base, "directive_type": kind, "start_hour": start, "end_hour": end,
           "value": None, "value_unit": "none"}
    if kind == "solar_reduction":
        pct = _percent(note)
        if pct is not None:
            reduction = re.search(r"reduction|reduced by|reduce by|drop(s|ping)? by|cut(s)? by|lower(ed)? by|decrease[sd]? by|loss of|% down|percent down|down by", t)
            out.update(value=pct, value_unit="percent_reduction" if reduction else "percent_remaining")
        else:
            for word, frac in sorted(FRACTIONS.items(), key=lambda kv: -len(kv[0])):
                if word in t:
                    reduced = re.search(r"(reduced|cut|lowered|decreased|down) by " + re.escape(word), t)
                    out.update(value=frac, value_unit="fraction_reduction" if reduced else "fraction_remaining")
                    break
            else:
                if re.search(r"offline|no solar|zero|completely|entirely|unavailable|nothing|no output|fully shaded", t):
                    out.update(value=0, value_unit="fraction_remaining")
                else:
                    return noop
    elif kind == "minimum_battery_reserve":
        kwh, pct = _kwh(note), _percent(note)
        if kwh is not None:
            out.update(value=kwh, value_unit="kwh")
        elif pct is not None:
            out.update(value=pct, value_unit="percent_of_capacity")
        elif "half" in t:
            out.update(value=0.5, value_unit="fraction_of_capacity")
        else:
            return noop
    elif kind == "max_grid_window":
        out.update(value=_kwh(note), value_unit="kwh")
    return out


def interpret_notes(notes: list[str]) -> list[dict]:
    return [{**interpret_note(n), "note_index": i} for i, n in enumerate(notes)]
