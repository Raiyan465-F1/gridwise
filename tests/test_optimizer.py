from app.optimizer import is_feasible, optimize
from app.schemas import parse_scenario
from app.validator import replay


def test_public_samples_reach_reference_optimal_cost(samples):
    for case in samples:
        inp, exp = case["input"], case["expected_output"]
        gt = exp["directive_interpretation"]
        plan = optimize(parse_scenario(inp), gt)
        resp = {"scenario_id": inp["scenario_id"], "directive_interpretation": gt,
                "plan_summary": "", **{k: plan[k] for k in
                                        ("hourly_plan", "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh")}}
        assert replay(inp, gt, resp) == [], case["id"]
        assert abs(plan["total_cost_bdt"] - exp["total_cost_bdt"]) <= 0.01, case["id"]


def test_reference_outputs_pass_validator(samples):
    for case in samples:
        exp = case["expected_output"]
        assert replay(case["input"], exp["directive_interpretation"], exp) == [], case["id"]


def test_non_integer_data_stays_valid(samples):
    import random
    rng = random.Random(7)
    checked = 0
    for case in samples:
        inp = {**case["input"]}
        inp["hours"] = [{**h, "demand_kwh": h["demand_kwh"] * rng.uniform(0.7, 1.3) + 0.123,
                         "solar_kwh": h["solar_kwh"] * rng.uniform(0.5, 1.5),
                         "tariff_bdt_per_kwh": h["tariff_bdt_per_kwh"] + rng.uniform(-0.5, 0.5)}
                        for h in inp["hours"]]
        gt = case["expected_output"]["directive_interpretation"]
        sc = parse_scenario(inp)
        if not is_feasible(sc, gt):  # random perturbation can break e.g. a grid cap
            continue
        checked += 1
        plan = optimize(sc, gt)
        resp = {"scenario_id": inp["scenario_id"], "directive_interpretation": gt, "plan_summary": "",
                **{k: plan[k] for k in ("hourly_plan", "total_grid_kwh", "total_cost_bdt", "peak_grid_kwh")}}
        assert replay(inp, gt, resp) == [], case["id"]
    assert checked >= 5
