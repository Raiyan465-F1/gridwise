import json

import pytest
from fastapi.testclient import TestClient

from app import interpreter
from app.llm import LLMError
from app.main import app
from app.validator import check_interpretation_schema, compare_interpretation, replay
from tests.conftest import gt_to_intermediate

client = TestClient(app)


@pytest.fixture(autouse=True)
def clear_cache():
    interpreter._cache.data.clear()
    yield


def patch_llm(monkeypatch, fn):
    monkeypatch.setattr(interpreter, "call_llm", fn)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok"}


def test_samples_end_to_end_with_perfect_llm(monkeypatch, samples):
    for case in samples:
        gt = case["expected_output"]["directive_interpretation"]

        async def fake(_msg, gt=gt):
            return gt_to_intermediate(gt)
        patch_llm(monkeypatch, fake)
        r = client.post("/optimize-energy", json=case["input"])
        assert r.status_code == 200, case["id"]
        body = r.json()
        assert check_interpretation_schema(case["input"], body) == []
        assert compare_interpretation(gt, body["directive_interpretation"]) == []
        assert replay(case["input"], gt, body) == []
        assert abs(body["total_cost_bdt"] - case["expected_output"]["total_cost_bdt"]) <= 0.01


def test_provider_down_uses_backup(monkeypatch, samples):
    async def down(_msg):
        raise LLMError("provider returned HTTP 529")
    patch_llm(monkeypatch, down)
    case = samples[5]
    r = client.post("/optimize-energy", json=case["input"])
    assert r.status_code == 200
    body = r.json()
    gt = case["expected_output"]["directive_interpretation"]
    assert replay(case["input"], gt, body) == []


def test_bad_llm_output_is_retried_then_accepted(monkeypatch, samples):
    case = samples[0]
    gt = case["expected_output"]["directive_interpretation"]
    calls = {"n": 0}

    async def flaky(msg):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"interpretations": [{"note_index": 0, "directive_type": "shut_down_campus"}]}
        assert "rejected" in msg
        return gt_to_intermediate(gt)
    patch_llm(monkeypatch, flaky)
    r = client.post("/optimize-energy", json=case["input"])
    assert r.status_code == 200 and calls["n"] == 2
    assert compare_interpretation(gt, r.json()["directive_interpretation"]) == []


def test_garbage_llm_never_invents_types(monkeypatch, samples):
    async def garbage(_msg):
        return {"interpretations": [{"note_index": 0, "directive_type": "delete_everything",
                                     "start_hour": 99}]}
    patch_llm(monkeypatch, garbage)
    case = samples[0]
    r = client.post("/optimize-energy", json=case["input"])
    assert r.status_code == 200
    assert check_interpretation_schema(case["input"], r.json()) == []


@pytest.mark.parametrize("payload,status", [
    ("{not json", 400),
    ("[]", 400),
    (json.dumps({"scenario_id": "x"}), 400),
])
def test_malformed(payload, status):
    r = client.post("/optimize-energy", content=payload, headers={"content-type": "application/json"})
    assert r.status_code == status
    assert "Traceback" not in r.text


def test_structural_and_semantic_errors(samples):
    base = samples[0]["input"]
    bad = {**base, "hours": base["hours"][:23]}
    assert client.post("/optimize-energy", json=bad).status_code == 400
    bad = {**base, "operator_notes": []}
    assert client.post("/optimize-energy", json=bad).status_code == 400
    bad = {**base, "operator_notes": ["a", "b", "c", "d"]}
    assert client.post("/optimize-energy", json=bad).status_code == 400
    bad = {**base, "battery": {**base["battery"], "capacity_kwh": "big"}}
    assert client.post("/optimize-energy", json=bad).status_code == 400
    bad = {**base, "battery": {**base["battery"], "initial_energy_kwh": 10_000}}
    assert client.post("/optimize-energy", json=bad).status_code == 422
