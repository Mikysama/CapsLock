from scripts.evaluate_memory import DEFAULT_FIXTURE, evaluate


def test_memory_quality_fixture_meets_top5_gate() -> None:
    result = evaluate(DEFAULT_FIXTURE)
    assert result["top5_hit_rate"] >= 0.90
    assert result["failures"] == []
