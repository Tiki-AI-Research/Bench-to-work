"""Task data model and Agent invocation protocol. New domain tasks should only need JSON + verifier."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePath
from typing import Any, Protocol

from .enums import Phase, Role
from .record_types import CallIdentity


class AdapterError(RuntimeError):
    """External Agent or command invocation failure; does not mean the evaluated Worker failed a task rubric item."""


class OutputProtocolError(AdapterError, ValueError):
    """Returned content violated the output protocol; role and phase are recorded by Runner."""


class ExternalExecutionError(AdapterError):
    """Confirmed process or network execution failure; must not be scored as a Worker rubric failure."""


class IncompleteResponseError(ExternalExecutionError):
    """Model call ended without completing delivery (e.g., hit max_tokens); never fabricate a reply from partial output."""


@dataclass
class Attachment:
    """Task definitions and replies use workspace-relative paths; adapters receive resolved paths."""

    path: str
    kind: str = "file"

    def __post_init__(self):
        if not isinstance(self.path, str) or not self.path.strip():
            raise ValueError("Attachment requires a non-empty path")
        if self.kind not in {"file", "image"}:
            raise ValueError("Attachment kind must be 'file' or 'image'")


def parse_attachments(values: list) -> list[Attachment]:
    """Support plain path-string shorthand; images must be declared explicitly without guessing file purpose."""
    if not isinstance(values, list):
        raise ValueError("attachments must be a list")
    attachments = []
    for value in values:
        if isinstance(value, Attachment):
            attachment = value
        elif isinstance(value, str):
            attachment = Attachment(value)
        elif isinstance(value, dict):
            attachment = Attachment(**value)
        else:
            raise ValueError(
                "Each attachment must be a path string, config dict, or Attachment"
            )
        attachments.append(attachment)
    return attachments


@dataclass
class UserMessage:
    """Three user message modes: fixed literal text, context-only generation, or read-only user-inbox generation."""

    type: str = "context"
    instruction: str | None = None
    text: str | None = None

    def __post_init__(self):
        if self.type == "fixed":
            if not isinstance(self.text, str) or not self.text.strip():
                raise ValueError("fixed message requires non-empty text")
            if self.instruction is not None:
                raise ValueError("fixed message cannot specify instruction")
        elif self.type in {"context", "workspace"}:
            if not isinstance(self.instruction, str) or not self.instruction.strip():
                raise ValueError("context / workspace message requires non-empty instruction")
            if self.text is not None:
                raise ValueError("context / workspace message cannot specify text")
        else:
            raise ValueError("Message type must be 'fixed', 'context', or 'workspace'")


@dataclass
class Action:
    """One permitted user turn; message generation config is kept separate from materials released on that turn."""

    id: str
    message: UserMessage
    attachments: list[Attachment] = field(default_factory=list)
    when: str = "Stage start"

    def __post_init__(self):
        if not isinstance(self.id, str) or not self.id.strip():
            raise ValueError("Action id must be a non-empty string")
        if isinstance(self.message, dict):
            self.message = UserMessage(**self.message)
        if not isinstance(self.message, UserMessage):
            raise ValueError("action.message must be a UserMessage configuration object")
        self.attachments = parse_attachments(self.attachments)


@dataclass
class Hook:
    """Task environment event: a command or registered handler executed by Runner at a defined lifecycle point."""

    id: str
    event: str
    command: list[str] = field(default_factory=list)
    description: str = ""
    stage_id: str | None = None
    turn: int | None = None
    action_id: str | None = None
    timeout: int = 60
    handler: str | None = None

    def __post_init__(self):
        if not isinstance(self.id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", self.id):
            raise ValueError("Invalid hook id")
        if self.event not in {
            "before_user",
            "after_worker",
            "after_stage",
            "after_run",
        }:
            raise ValueError(f"Unknown hook event: {self.event}")
        if not isinstance(self.command, list) or not all(
            isinstance(arg, str) and arg for arg in self.command
        ):
            raise ValueError("hook command must be a list of non-empty argument strings")
        if self.handler is not None and (
            not isinstance(self.handler, str) or not self.handler.strip()
        ):
            raise ValueError("hook handler must be a non-empty name")
        if bool(self.command) == (self.handler is not None):
            raise ValueError("hook must specify exactly one of command or handler")
        if not isinstance(self.description, str) or not self.description.strip():
            raise ValueError("hook requires a non-empty description")
        if self.turn is not None and (type(self.turn) is not int or self.turn < 1):
            raise ValueError("hook turn must be a positive integer or null")
        if type(self.timeout) is not int or self.timeout < 1:
            raise ValueError("hook timeout must be a positive integer")
        if self.event == "after_run":
            if any(
                value is not None
                for value in (self.stage_id, self.turn, self.action_id)
            ):
                raise ValueError("after_run hook cannot scope to stage_id, turn, or action_id")
        elif not self.stage_id:
            raise ValueError("In-stage hook must specify stage_id")


@dataclass
class Check:
    """Check identifier, rubric description, and gate flag; programmatic checks need only these fields."""

    id: str
    description: str
    hard_gate: bool = True

    def __post_init__(self):
        if (
            not isinstance(self.id, str)
            or not self.id.strip()
            or not isinstance(self.description, str)
            or not self.description.strip()
            or type(self.hard_gate) is not bool
        ):
            raise ValueError(
                "Check requires non-empty string id and description, and a boolean hard_gate"
            )


@dataclass
class Criterion(Check):
    """Semantic rubric item: optional verification guidance tells Reviewer where and how to inspect evidence."""

    verification: str = ""

    def __post_init__(self):
        super().__post_init__()
        if not isinstance(self.verification, str):
            raise ValueError("verification must be a string")


@dataclass
class Stage:
    id: str
    title: str
    initial: Action
    checkpoint: str
    verifier_checks: list[Check]
    criteria: list[Criterion]
    continuations: list[Action] = field(default_factory=list)
    max_turns: int = 3
    # Public conversation is always evaluated; when true, Worker submission files are also snapshotted for review.
    needs_workspace: bool = True
    retry_limit: int = 0
    default_reply: UserMessage = field(
        default_factory=lambda: UserMessage(
            instruction=(
                "Inform the assistant that you currently have no additional information to provide; "
                "respond naturally to the current question without guessing the answer."
            )
        )
    )

    def __post_init__(self):
        if isinstance(self.default_reply, dict):
            self.default_reply = UserMessage(**self.default_reply)
        if not isinstance(self.default_reply, UserMessage):
            raise ValueError("default_reply must be a UserMessage configuration object")
        if not isinstance(self.id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", self.id):
            raise ValueError(f"Invalid stage id: {self.id}")
        if type(self.max_turns) is not int or self.max_turns < 1:
            raise ValueError("max_turns must be greater than 0")
        if type(self.needs_workspace) is not bool:
            raise ValueError(f"needs_workspace for {self.id} must be a JSON boolean")
        if type(self.retry_limit) is not int or self.retry_limit < 0:
            raise ValueError(f"retry_limit for {self.id} must be a non-negative integer")
        if not self.needs_workspace and self.verifier_checks:
            raise ValueError(
                f"{self.id} evaluates conversation only and cannot configure file verifier_checks"
            )
        if not self.verifier_checks and not self.criteria:
            raise ValueError(
                f"{self.id} requires at least one programmatic or semantic check"
            )
        for group in [
            self.verifier_checks,
            self.criteria,
            [self.initial, *self.continuations],
        ]:
            if len({item.id for item in group}) != len(group):
                raise ValueError(f"{self.id} contains duplicate check or action ids")
        for action in [self.initial, *self.continuations]:
            if action.id == "default_reply" or re.fullmatch(r"retry_\d+", action.id):
                raise ValueError(
                    f"Action id in {self.id} uses a reserved framework name: {action.id}"
                )


@dataclass
class Task:
    id: str
    title: str
    persona: str
    background: str
    stages: list[Stage]
    verifier: list[str]
    root: Path
    submission_paths: list[str] = field(default_factory=lambda: ["."])
    hooks: list[Hook] = field(default_factory=list)

    def __post_init__(self):
        # Cross-stage uniqueness and hook target references require the complete Task definition.
        if not self.stages:
            raise ValueError("Task requires at least one stage")
        stages_by_id = {stage.id: stage for stage in self.stages}
        if len(stages_by_id) != len(self.stages):
            raise ValueError("Duplicate stage ids in task")
        if not isinstance(self.verifier, list) or not all(
            isinstance(arg, str) and arg for arg in self.verifier
        ):
            raise ValueError("verifier must be a list of command argument strings")
        if any(stage.verifier_checks for stage in self.stages) and not self.verifier:
            raise ValueError("Task with verifier_checks must provide a verifier command")
        if (
            not isinstance(self.submission_paths, list)
            or not self.submission_paths
            or not all(isinstance(path, str) and path for path in self.submission_paths)
        ):
            raise ValueError("submission_paths must be a non-empty list of relative paths")
        for path in self.submission_paths:
            inside(self.root, path)
        if len({hook.id for hook in self.hooks}) != len(self.hooks):
            raise ValueError("Duplicate hook ids in task")
        for hook in self.hooks:
            if hook.event == "after_run":
                continue
            stage = stages_by_id.get(hook.stage_id)
            if stage is None or (hook.turn is not None and hook.turn > stage.max_turns):
                raise ValueError(
                    f"Hook {hook.id} references a missing stage or exceeds max_turns"
                )
            if hook.action_id is not None and hook.action_id not in {
                stage.initial.id,
                "default_reply",
                *[action.id for action in stage.continuations],
                *[f"retry_{n}" for n in range(1, stage.retry_limit + 1)],
            }:
                raise ValueError(f"Hook {hook.id} references an undefined action_id")


def inside(root: Path, relative: str) -> Path:
    """Allow only relative paths that resolve inside root; blocks path traversal and escaping symlinks."""
    p = (root / relative).resolve()
    if Path(relative).is_absolute() or not p.is_relative_to(root.resolve()):
        raise ValueError(f"Path escapes root directory: {relative}")
    return p


def load_task(path: Path) -> Task:
    """Convert task JSON into validated model objects and verify referenced attachment files exist."""
    raw = json.loads(path.read_text(encoding="utf-8"))
    valid_fields = {
        "id",
        "title",
        "persona",
        "background",
        "stages",
        "verifier",
        "submission_paths",
        "hooks",
        "root",
    }
    if unknown := sorted(set(raw) - valid_fields):
        raise ValueError(f"Unsupported task field(s): {', '.join(unknown)}")
    stages = []
    for definition in raw["stages"]:
        fields = dict(definition)
        initial = Action(**fields.pop("initial"))
        continuations = [Action(**item) for item in fields.pop("continuations", [])]
        verifier_checks = [Check(**item) for item in fields.pop("verifier_checks")]
        criteria = [Criterion(**item) for item in fields.pop("criteria")]
        stages.append(
            Stage(
                **fields,
                initial=initial,
                continuations=continuations,
                verifier_checks=verifier_checks,
                criteria=criteria,
            )
        )
    task = Task(
        id=raw["id"],
        title=raw["title"],
        persona=raw["persona"],
        background=raw["background"],
        stages=stages,
        verifier=raw.get("verifier", []),
        root=path.parent.resolve(),
        submission_paths=raw.get("submission_paths", ["."]),
        hooks=[Hook(**item) for item in raw.get("hooks", [])],
    )
    for stage in task.stages:
        for action in [stage.initial, *stage.continuations]:
            for attachment in action.attachments:
                if not inside(task.root, attachment.path).is_file():
                    raise ValueError(f"Attachment file does not exist: {attachment.path}")
    return task


@dataclass
class Call:
    """Pass invocation inputs separately; each adapter decides how to manage sessions and history."""

    role: Role | str
    prompt: str
    # None indicates a tool-less call; explicit image attachments may still be passed.
    workspace: PurePath | None
    log_dir: Path
    session_id: str | None = None
    schema: dict[str, Any] | None = None
    # Public conversation prior to this prompt, excluding harness-internal tool traces.
    history: list[dict[str, Any]] = field(default_factory=list)
    instructions: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    # Call identity is for adapter logging and tracing only, not injected into prompts.
    run_id: str | None = None
    call_id: str | None = None
    stage_id: str | None = None
    turn_id: str | None = None
    turn: int | None = None
    retry_count: int = 0
    phase: Phase | str = Phase.UNSPECIFIED
    # Reviewer protocol retries are tracked separately from Worker business retry_count.
    protocol_retry_count: int = 0
    protocol_retry_of: str | None = None

    @property
    def identity(self) -> CallIdentity:
        """Shared call identity for command wrappers and gateway adapters, without prompts or rubrics."""
        return {
            "run_id": self.run_id,
            "call_id": self.call_id,
            "stage_id": self.stage_id,
            "turn_id": self.turn_id,
            "turn": self.turn,
            "retry_count": self.retry_count,
            "phase": self.phase,
            "protocol_retry_count": self.protocol_retry_count,
            "protocol_retry_of": self.protocol_retry_of,
        }


@dataclass
class Reply:
    """Returned text and trace references; file deliverables remain in the workspace for formal snapshotting."""

    text: str
    session_id: str | None = None
    # Opaque trace IDs or record URIs returned by the adapter.
    trace_refs: list[str] = field(default_factory=list)

    def __post_init__(self):
        if not isinstance(self.text, str) or not self.text.strip():
            raise OutputProtocolError("Agent must return non-empty text")
        if self.session_id is not None and not isinstance(self.session_id, str):
            raise OutputProtocolError("session_id must be a string or null")
        if not isinstance(self.trace_refs, list) or not all(
            isinstance(ref, str) and ref.strip() for ref in self.trace_refs
        ):
            raise OutputProtocolError("trace_refs must be a list of non-empty strings")


class Agent(Protocol):
    def run(self, call: Call) -> Reply: ...


def object_schema(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


TEXT = {"type": "string", "minLength": 1}
CHECK_SCHEMA = object_schema(
    {"id": TEXT, "passed": {"type": "boolean"}, "evidence": TEXT}
)
PROGRESS_SCHEMA = object_schema(
    {
        # Progress review checks only whether dialogue has reached evaluation, not file quality.
        "reason": TEXT,
        "decision": {"enum": ["request_user_reply", "review_submission", "error"]},
        "continuation_id": {"type": ["string", "null"]},
    }
)
REVIEW_SCHEMA = object_schema(
    {
        "reason": TEXT,
        # Schema lists evidence before passed; JSON validation matches property names regardless of key order.
        "criteria": {
            "type": "array",
            "items": object_schema(
                {"id": TEXT, "evidence": TEXT, "passed": {"type": "boolean"}}
            ),
        },
    }
)
VERIFIER_SCHEMA = object_schema(
    {
        "checks": {"type": "array", "items": CHECK_SCHEMA},
        "observations": {"type": "object"},
    }
)
