"""Explicit promotion gate for opt-in filtered tool experiments."""

from statistics import median


def promotion_gate(
    *,
    required_tools: int,
    recalled_tools: int,
    full_schema_tokens: list[int],
    selected_schema_tokens: list[int],
    hard_gate_failures: int,
    paired_resolve_ci95: tuple[float, float] | None,
    live_confirmed: bool,
) -> dict:
    if not 0 <= recalled_tools <= required_tools or hard_gate_failures < 0:
        raise ValueError("invalid tool recall or hard gate counts")
    if len(full_schema_tokens) != len(selected_schema_tokens) or any(
        value < 0 for value in (*full_schema_tokens, *selected_schema_tokens)
    ):
        raise ValueError("schema token samples must be nonnegative and paired")
    recall = recalled_tools / required_tools if required_tools else None
    baseline = median(full_schema_tokens) if full_schema_tokens else 0
    reduction = 1 - median(selected_schema_tokens) / baseline if baseline else None
    noninferior = paired_resolve_ci95 is not None and paired_resolve_ci95[0] >= -0.02
    eligible = (
        hard_gate_failures == 0
        and recall is not None
        and recall >= 0.99
        and reduction is not None
        and reduction >= 0.20
        and live_confirmed
        and noninferior
    )
    return {
        "required_tool_recall": recall,
        "schema_token_median_reduction": reduction,
        "hard_gate_failures": hard_gate_failures,
        "live_confirmed": live_confirmed,
        "noninferior_at_minus_2pp": noninferior,
        "eligible_for_promotion": eligible,
        "recommended_mode": "filtered" if eligible else "shadow",
    }
