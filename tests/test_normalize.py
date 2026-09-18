from types import SimpleNamespace

import pytest

from app.normalize import InterpretationError, build_hours, normalize_all, normalize_one

SC = SimpleNamespace(battery=SimpleNamespace(capacity_kwh=200), operator_notes=["a", "b"])


def item(**kw):
    base = {"note_index": 0, "directive_type": "solar_reduction", "start_hour": 13, "end_hour": 15,
            "hours_list": None, "value": 20, "value_unit": "percent_remaining", "explanation": "x"}
    base.update(kw)
    return base


def test_hours_exclusive_end_and_wrap():
    assert build_hours(13, 15, None) == [13, 14]
    assert build_hours(22, 2, None) == [0, 1, 22, 23]
    assert build_hours(21, 24, None) == [21, 22, 23]
    assert build_hours(20, 0, None) == [20, 21, 22, 23]
    assert build_hours(0, 24, None) == list(range(24))
    assert build_hours(None, None, [17, 9, 9]) == [9, 17]


def test_units():
    assert normalize_one(item(), SC)["structured_adjustment"]["factor"] == 0.2
    assert normalize_one(item(value=80, value_unit="percent_reduction"), SC)["structured_adjustment"]["factor"] == 0.2
    assert normalize_one(item(value=0.25, value_unit="fraction_remaining"), SC)["structured_adjustment"]["factor"] == 0.25
    r = normalize_one(item(directive_type="minimum_battery_reserve", value=50, value_unit="percent_of_capacity"), SC)
    assert r["structured_adjustment"] == {"hours": [13, 14], "minimum_energy_kwh": 100.0}


def test_guardrails_reject_bad_output():
    with pytest.raises(InterpretationError):
        normalize_one(item(directive_type="turn_off_hvac"), SC)
    with pytest.raises(InterpretationError):
        normalize_one(item(value=150, value_unit="percent_remaining"), SC)
    with pytest.raises(InterpretationError):
        normalize_one(item(directive_type="minimum_battery_reserve", value=500, value_unit="kwh"), SC)
    with pytest.raises(InterpretationError):
        normalize_one(item(start_hour=30), SC)
    with pytest.raises(InterpretationError):
        normalize_all({"interpretations": [item()]}, SC)  # one note missing
    with pytest.raises(InterpretationError):
        normalize_all({"interpretations": [item(), item()]}, SC)  # duplicate index


def test_no_op_shape():
    r = normalize_one(item(directive_type="no_op"), SC)
    assert r["applies"] is False and r["structured_adjustment"] is None and r["directive_type"] == "no_op"
