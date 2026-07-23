"""Local, bounded parent/child Agent collaboration primitives."""

from .models import (
    AgentMessage,
    AgentMessageKind,
    AgentTaskContract,
    AgentTaskState,
    CapabilityGrant,
    CapabilityKind,
    ValidatedAgentOutput,
    VerificationRequirement,
)
from .verifier import AgentOutputVerifier, VerificationError
from .workspace import AgentWorkspaceManager, ScopedWorkspacePolicy, WorkspaceSnapshot
from .service import ChildApprovalPending, CollaborationService
from .capabilities import ChildCapabilityPolicy
from .runner import ChildAgentRunner

__all__ = [
    "AgentMessage",
    "AgentMessageKind",
    "AgentOutputVerifier",
    "AgentTaskContract",
    "AgentTaskState",
    "AgentWorkspaceManager",
    "CollaborationService",
    "CapabilityGrant",
    "CapabilityKind",
    "ChildApprovalPending",
    "ChildCapabilityPolicy",
    "ChildAgentRunner",
    "ValidatedAgentOutput",
    "VerificationError",
    "VerificationRequirement",
    "WorkspaceSnapshot",
    "ScopedWorkspacePolicy",
]
