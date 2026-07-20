"""Provider routing, metering, and budget domain types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ModelRole(StrEnum):
    REASONING = "reasoning"
    FAST = "fast"
    EMBEDDING = "embedding"
    VISION = "vision"


class ModelErrorCode(StrEnum):
    UNAVAILABLE = "model_unavailable"
    RATE_LIMITED = "model_rate_limited"
    DATA_POLICY_MISMATCH = "model_data_policy_mismatch"
    BUDGET_EXCEEDED = "model_budget_exceeded"
    AUTHENTICATION = "model_authentication_failed"
    INVALID_REQUEST = "model_invalid_request"


@dataclass(frozen=True)
class BudgetRequest:
    run_id: str
    scope: str
    limit_type: str
    current_value: float
    reserved_value: float
    limit_value: float
    profile: str


class ModelRoutingError(RuntimeError):
    code = ModelErrorCode.UNAVAILABLE


class ModelBudgetExceeded(ModelRoutingError):
    code = ModelErrorCode.BUDGET_EXCEEDED


class ModelDataPolicyMismatch(ModelRoutingError):
    code = ModelErrorCode.DATA_POLICY_MISMATCH
