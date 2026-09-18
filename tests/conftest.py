import json
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key-not-real")  # fake LLM is patched in tests


@pytest.fixture(scope="session")
def samples():
    return json.loads((ROOT / "data" / "public_sample_cases.json").read_text())["cases"]


def gt_to_intermediate(gt: list[dict]) -> dict:
    """Turn organizer ground truth into what a perfect LLM would return."""
    items = []
    for i, d in enumerate(gt):
        t, adj = d["directive_type"], d["structured_adjustment"]
        item = {"note_index": i, "directive_type": t, "start_hour": None, "end_hour": None,
                "hours_list": None, "value": None, "value_unit": "none", "explanation": "test"}
        if adj:
            item["hours_list"] = adj["hours"]
            if t == "solar_reduction":
                item.update(value=adj["factor"], value_unit="fraction_remaining")
            elif t == "minimum_battery_reserve":
                item.update(value=adj["minimum_energy_kwh"], value_unit="kwh")
            elif t == "max_grid_window":
                item.update(value=adj["max_grid_kwh"], value_unit="kwh")
        items.append(item)
    return {"interpretations": items}
