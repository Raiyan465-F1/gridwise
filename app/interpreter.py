"""Operator-note interpretation pipeline.

    LLM (primary)  ->  deterministic guardrails (normalize.py)  ->  feasibility check
       | invalid / infeasible: retry once with validator feedback
       | provider down / still invalid: rule-based backup parser (same guardrails)
       | still unusable for a note: that note degrades to a safe no_op

The service never passes an unvalidated or unsupported directive to the optimizer.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict

from . import fallback_parser
from .config import settings
from .llm import LLMError, call_llm
from .normalize import InterpretationError, normalize_all, normalize_one
from .optimizer import is_feasible
from .prompt import build_user_message
from .schemas import Scenario

log = logging.getLogger("gridwise.interpreter")

TOTAL_LLM_BUDGET_S = 20.0  # keep the whole request under the 30 s judge timeout


class _LRU:
    def __init__(self, size: int):
        self.size = size
        self.data: OrderedDict = OrderedDict()

    def get(self, key):
        if key in self.data:
            self.data.move_to_end(key)
            return self.data[key]
        return None

    def put(self, key, value):
        if self.size <= 0:
            return
        self.data[key] = value
        self.data.move_to_end(key)
        while len(self.data) > self.size:
            self.data.popitem(last=False)


_cache = _LRU(settings.cache_size)


def _extract_items(raw) -> list:
    if isinstance(raw, dict):
        raw = raw.get("interpretations", raw.get("directives", []))
    return raw if isinstance(raw, list) else []


def _best_effort(sc: Scenario, llm_raw: list | None, warnings: list[str]) -> list[dict]:
    """Per-note salvage: LLM item if valid, else backup-parser item, else safe no_op."""
    llm_by_idx = {}
    for pos, it in enumerate(llm_raw or []):
        if isinstance(it, dict):
            idx = it.get("note_index", pos)
            if isinstance(idx, int) and idx not in llm_by_idx:
                llm_by_idx[idx] = it
    backup = fallback_parser.interpret_notes(sc.operator_notes) if settings.enable_rule_fallback else []
    out = []
    for i, note in enumerate(sc.operator_notes):
        chosen = None
        for source, item in (("llm", llm_by_idx.get(i)), ("backup", backup[i] if backup else None)):
            if item is None:
                continue
            try:
                chosen = normalize_one({**item, "note_index": i}, sc)
                if source == "backup":
                    warnings.append(f"note {i}: interpreted by backup parser")
                break
            except (InterpretationError, KeyError, TypeError, ValueError):
                continue
        if chosen is None:
            warnings.append(f"note {i}: could not be interpreted safely; treated as no_op")
            chosen = normalize_one({"note_index": i, "directive_type": "no_op",
                                    "explanation": "Note could not be interpreted safely; no rule applied."}, sc)
        out.append(chosen)
    return out


def _backup_directive(sc: Scenario, i: int) -> dict | None:
    if not settings.enable_rule_fallback:
        return None
    try:
        return normalize_one(fallback_parser.interpret_notes(sc.operator_notes)[i], sc)
    except (InterpretationError, KeyError, TypeError, ValueError):
        return None


def _make_feasible(sc: Scenario, directives: list[dict], warnings: list[str]) -> list[dict]:
    """Organizer scenarios are feasible, so infeasibility means a misread note.
    Repair one note at a time: first try the backup reading of that note, then no_op."""
    if is_feasible(sc, directives):
        return directives
    active = [i for i, d in enumerate(directives) if d["directive_type"] != "no_op"]
    for i in active:
        alt = _backup_directive(sc, i)
        if alt is not None and alt != directives[i]:
            trial = list(directives)
            trial[i] = alt
            if is_feasible(sc, trial):
                warnings.append(f"note {i}: replaced by backup interpretation to restore feasibility")
                return trial
    for i in active:
        trial = list(directives)
        trial[i] = normalize_one({"note_index": i, "directive_type": "no_op",
                                  "explanation": "Directive made the schedule infeasible and was not applied."}, sc)
        if is_feasible(sc, trial):
            warnings.append(f"note {i}: directive dropped to restore feasibility")
            return trial
    warnings.append("all directives dropped to restore feasibility")
    return [normalize_one({"note_index": i, "directive_type": "no_op",
                           "explanation": "Directive not applied (infeasible combination)."}, sc)
            for i in range(len(directives))]


async def interpret(sc: Scenario) -> tuple[list[dict], dict]:
    t0 = time.monotonic()
    warnings: list[str] = []
    key = tuple(sc.operator_notes)
    meta = {"source": "llm", "attempts": 0, "cached": False, "warnings": warnings}

    cached = _cache.get(key)
    if cached is not None:
        try:
            directives = normalize_all(cached, sc)
            if await asyncio.to_thread(is_feasible, sc, directives):
                meta["cached"] = True
                return directives, meta
        except InterpretationError:
            pass

    feedback = None
    last_raw = None
    if settings.llm_configured:
        for attempt in range(1, max(1, settings.llm_max_attempts) + 1):
            remaining = TOTAL_LLM_BUDGET_S - (time.monotonic() - t0)
            if remaining < 2:
                break
            meta["attempts"] = attempt
            try:
                raw = await asyncio.wait_for(call_llm(build_user_message(sc, feedback)),
                                             timeout=min(settings.llm_timeout_s, remaining))
            except asyncio.TimeoutError:
                log.warning("LLM attempt %d timed out", attempt)
                feedback = None
                continue
            except LLMError as exc:
                log.warning("LLM attempt %d failed: %s", attempt, exc)
                if not exc.retryable:
                    break
                # Respect provider's Retry-After / rate-limit hint if it gave one.
                wait_s = exc.retry_after_s
                if wait_s and wait_s > 0 and wait_s < remaining:
                    log.info("LLM attempt %d: backing off %.1fs per provider hint", attempt, wait_s)
                    await asyncio.sleep(wait_s)
                continue
            except Exception as exc:  # never let provider quirks crash the request
                log.warning("LLM attempt %d unexpected error: %s", attempt, type(exc).__name__)
                continue
            last_raw = _extract_items(raw)
            try:
                directives = normalize_all(raw, sc)
            except InterpretationError as exc:
                feedback = str(exc)
                log.info("LLM output rejected by guardrails: %s", exc)
                continue
            if not await asyncio.to_thread(is_feasible, sc, directives):
                feedback = ("the extracted directives make the 24-hour schedule infeasible; "
                            "re-check the hour windows (end hour is exclusive) and the numeric values")
                log.info("LLM directives infeasible; retrying")
                continue
            _cache.put(key, raw)
            return directives, meta
    else:
        warnings.append("LLM provider not configured")

    meta["source"] = "llm+backup" if last_raw else "backup"
    directives = _best_effort(sc, last_raw, warnings)
    directives = await asyncio.to_thread(_make_feasible, sc, directives, warnings)
    return directives, meta
