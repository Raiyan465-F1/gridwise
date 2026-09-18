import copy
import random

import pytest
from fastapi.testclient import TestClient

from app import interpreter
from app.llm import LLMError
from app.main import app
from app.validator import check_interpretation_schema, replay
from tests.conftest import gt_to_intermediate

client = TestClient(app)


@pytest.fixture(autouse=True)
def perfect_llm(monkeypatch):
    interpreter._cache.data.clear()
    state = {"gt": [], "calls": 0}

    async def fake(_msg):
        state["calls"] += 1
        return gt_to_intermediate(state["gt"])
    monkeypatch.setattr(interpreter, "call_llm", fake)
    return state


def post_and_check(inp, gt):
    r = client.post("/optimize-energy", json=inp)
    assert r.status_code == 200, r.text
    body = r.json()
    assert check_interpretation_schema(inp, body) == []
    assert replay(inp, gt, body) == []
    return body


def noop_gt(n=1):
    return [{"directive_type": "no_op", "applies": False, "structured_adjustment": None}] * n


def test_zero_size_battery(samples, perfect_llm):
    inp = copy.deepcopy(samples[0]["input"])
    inp["battery"] = {"capacity_kwh": 0, "initial_energy_kwh": 0, "minimum_energy_kwh": 0,
                      "max_charge_kwh_per_hour": 0, "max_discharge_kwh_per_hour": 0}
    perfect_llm["gt"] = noop_gt(2)
    body = post_and_check(inp, perfect_llm["gt"])
    assert all(p["battery_action"] == "idle" for p in body["hourly_plan"])


def test_zero_tariff_and_zero_solar(samples, perfect_llm):
    inp = copy.deepcopy(samples[1]["input"])
    for h in inp["hours"]:
        h["solar_kwh"] = 0
        h["tariff_bdt_per_kwh"] = 0
    perfect_llm["gt"] = samples[1]["expected_output"]["directive_interpretation"]
    body = post_and_check(inp, perfect_llm["gt"])
    assert body["total_cost_bdt"] == 0


def test_shuffled_hours_extra_fields_float_hours(samples, perfect_llm):
    case = samples[6]
    inp = copy.deepcopy(case["input"])
    random.Random(1).shuffle(inp["hours"])
    for h in inp["hours"]:
        h["hour"] = float(h["hour"])
        h["note"] = "extra field"
    inp["extra_top_level"] = {"ignored": True}
    perfect_llm["gt"] = case["expected_output"]["directive_interpretation"]
    body = post_and_check(inp, perfect_llm["gt"])
    assert [p["hour"] for p in body["hourly_plan"]] == list(range(24))
    assert abs(body["total_cost_bdt"] - case["expected_output"]["total_cost_bdt"]) <= 0.01


def test_fractional_values(samples, perfect_llm):
    inp = copy.deepcopy(samples[9]["input"])
    for h in inp["hours"]:
        h["demand_kwh"] += 0.337
        h["tariff_bdt_per_kwh"] += 0.25
    perfect_llm["gt"] = samples[9]["expected_output"]["directive_interpretation"]
    post_and_check(inp, perfect_llm["gt"])


def test_scenario_id_echoed_exactly(samples, perfect_llm):
    inp = copy.deepcopy(samples[0]["input"])
    inp["scenario_id"] = "  weird id ✓ "
    perfect_llm["gt"] = samples[0]["expected_output"]["directive_interpretation"]
    assert post_and_check(inp, perfect_llm["gt"])["scenario_id"] == "  weird id ✓ "


def test_repeat_request_uses_cache(samples, perfect_llm):
    perfect_llm["gt"] = samples[2]["expected_output"]["directive_interpretation"]
    for _ in range(3):
        post_and_check(samples[2]["input"], perfect_llm["gt"])
    assert perfect_llm["calls"] == 1


def test_auth_error_is_not_retried(samples, monkeypatch):
    calls = {"n": 0}

    async def unauthorized(_msg):
        calls["n"] += 1
        raise LLMError("provider returned HTTP 401", retryable=False)
    monkeypatch.setattr(interpreter, "call_llm", unauthorized)
    case = samples[3]
    r = client.post("/optimize-energy", json=case["input"])
    assert r.status_code == 200 and calls["n"] == 1
    assert replay(case["input"], case["expected_output"]["directive_interpretation"], r.json()) == []


def test_infeasible_llm_directive_is_dropped_not_crashing(samples, monkeypatch):
    case = samples[4]  # grid cap sample

    async def impossible(_msg):  # grid cap of 0 in the evening cannot be met
        return {"interpretations": [{"note_index": 0, "directive_type": "max_grid_window",
                                     "start_hour": 0, "end_hour": 24, "value": 0, "value_unit": "kwh",
                                     "explanation": "x"}]}
    monkeypatch.setattr(interpreter, "call_llm", impossible)
    r = client.post("/optimize-energy", json=case["input"])
    assert r.status_code == 200
    body = r.json()
    assert check_interpretation_schema(case["input"], body) == []
    # the backup reading of the note (155 kWh cap, 18-20) is feasible and replaces the bad one
    gt = case["expected_output"]["directive_interpretation"]
    assert body["directive_interpretation"][0]["structured_adjustment"] == gt[0]["structured_adjustment"]
    assert replay(case["input"], gt, body) == []


def test_wrong_method_and_unknown_route():
    assert client.get("/optimize-energy").status_code == 405
    assert client.get("/nope").status_code == 404
