"""Bounded enum values for call records; StrEnum preserves plain string serialization for JSON and adapters."""

from enum import StrEnum


class Role(StrEnum):
    """Agent invocation role, distinct from chat message roles ('user' / 'assistant')."""

    USER = "user"
    WORKER = "worker"
    REVIEWER = "reviewer"


class Phase(StrEnum):
    """Execution step within a turn or run, used for call records and error attribution."""

    UNSPECIFIED = ""
    RELEASE_MATERIALS = "release_materials"
    BEFORE_USER = "before_user"
    USER_ATTACHMENTS = "user_attachments"
    USER_MESSAGE = "user_message"
    WORKER = "worker"
    AFTER_WORKER = "after_worker"
    PROGRESS_REVIEW = "progress_review"
    PREPARE_SUBMISSION = "prepare_submission"
    VERIFICATION = "verification"
    EVALUATION = "evaluation"
    STAGE_TRANSITION = "stage_transition"
    AFTER_STAGE = "after_stage"
    AFTER_RUN = "after_run"
    AGENT_CLEANUP = "agent_cleanup"

    @property
    def agent_role(self) -> Role | None:
        """Return the Agent role for model-invocation phases; file, verifier, and scheduling phases return None."""
        match self:
            case Phase.USER_MESSAGE:
                return Role.USER
            case Phase.WORKER:
                return Role.WORKER
            case Phase.PROGRESS_REVIEW | Phase.EVALUATION:
                return Role.REVIEWER
            case _:
                return None


class CallStatus(StrEnum):
    """Adapter call transport status; 'returned' does not imply protocol validity or rubric pass."""

    RUNNING = "running"
    RETURNED = "returned"
    ERROR = "error"


class ErrorCategory(StrEnum):
    """Explicit failure category for diagnostics; does not assign training rewards or causal blame."""

    OUTPUT_PROTOCOL = "output_protocol"
    EXTERNAL_EXECUTION = "external_execution"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"
