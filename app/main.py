"""GridWise LLM-assisted energy optimizer — HTTP API.

GET  /health           -> {"status": "ok"}
POST /optimize-energy  -> directive_interpretation + 24-hour hourly_plan
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import settings
from .interpreter import interpret
from .llm import close_client
from .optimizer import InfeasibleError, optimize
from .schemas import RequestError, parse_scenario

logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("gridwise")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    log.info("GridWise starting (provider=%s, model=%s, llm_configured=%s, backup_parser=%s)",
             settings.llm_provider, settings.llm_model, settings.llm_configured,
             settings.enable_rule_fallback)
    yield
    await close_client()


app = FastAPI(title="GridWise LLM Energy Optimizer", version="1.0.0", lifespan=lifespan)


def _error(status: int, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": message})


@app.exception_handler(RequestValidationError)
async def _validation_handler(_req, _exc):
    return _error(400, "invalid request")


@app.exception_handler(StarletteHTTPException)
async def _http_handler(_req, exc: StarletteHTTPException):
    return _error(exc.status_code, str(exc.detail))


@app.exception_handler(Exception)
async def _unhandled(_req, exc: Exception):
    log.error("unhandled error: %s", type(exc).__name__)  # no stack trace / secrets in response
    return _error(500, "internal error")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/")
async def root():
    return {"service": "gridwise", "endpoints": ["GET /health", "POST /optimize-energy"]}


def _summary(plan: dict, directives: list[dict], tariff: list[float]) -> str:
    hp = plan["hourly_plan"]
    ch = [p["hour"] for p in hp if p["battery_action"] == "charge"]
    dis = [p["hour"] for p in hp if p["battery_action"] == "discharge"]
    applied = [d["directive_type"] for d in directives if d["applies"]]
    parts = [f"Minimum-cost 24-hour plan via linear programming: total grid {plan['total_grid_kwh']:g} kWh, "
             f"cost {plan['total_cost_bdt']:g} BDT, peak {plan['peak_grid_kwh']:g} kWh."]
    if ch or dis:
        parts.append(f"Battery charges in hours {ch or 'none'} (cheaper tariff/solar) and discharges in hours "
                     f"{dis or 'none'} (pricier tariff), returning to its initial level.")
    else:
        parts.append("Battery stays idle; shifting energy would not reduce cost.")
    parts.append(f"Applied directives: {', '.join(applied)}." if applied
                 else "No operator note changed the schedule.")
    return " ".join(parts)


@app.post("/optimize-energy")
async def optimize_energy(request: Request):
    t0 = time.monotonic()
    raw = await request.body()
    try:
        body = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error(400, "malformed JSON")
    try:
        sc = parse_scenario(body)
    except RequestError as exc:
        return _error(exc.status, exc.message)

    directives, meta = await interpret(sc)
    try:
        plan = await run_in_threadpool(optimize, sc, directives)
    except InfeasibleError:
        # Base scenario itself cannot satisfy the GridWise rules (e.g. demand impossible
        # to meet). Well-formed but semantically unsatisfiable -> 422.
        return _error(422, "scenario is infeasible under the GridWise energy rules")

    log.info("scenario=%s notes=%d source=%s attempts=%d cached=%s warnings=%d ms=%d",
             sc.scenario_id, len(sc.operator_notes), meta["source"], meta["attempts"],
             meta["cached"], len(meta["warnings"]), (time.monotonic() - t0) * 1000)

    return {
        "scenario_id": sc.scenario_id,
        "directive_interpretation": directives,
        "hourly_plan": plan["hourly_plan"],
        "total_grid_kwh": plan["total_grid_kwh"],
        "total_cost_bdt": plan["total_cost_bdt"],
        "peak_grid_kwh": plan["peak_grid_kwh"],
        "plan_summary": _summary(plan, directives, sc.tariff),
    }
