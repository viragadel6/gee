from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, Field

ChatRole = Literal["system", "user", "assistant", "tool"]


class ToolCall(BaseModel):
    id: str
    type: str = "function"
    function_name: str
    function_arguments: str


class ChatMessage(BaseModel):
    role: str
    content: str = ""
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None

    def to_provider_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            payload["tool_calls"] = self.tool_calls
        if self.tool_call_id:
            payload["tool_call_id"] = self.tool_call_id
        return payload


class ToolStatus(str, Enum):
    COMPLETED = "completed"
    ERROR = "error"
    INTERRUPTED = "interrupted"
    UNKNOWN = "unknown"


class TerminalState(str, Enum):
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RUNTIME_LIMIT = "runtime_limit"
    STEP_LIMIT = "step_limit"
    CONTEXT_LIMIT = "context_limit"


class AgentEventType(str, Enum):
    PROGRESS = "progress"
    DELTA = "delta"
    AGENT_EVENT = "agent_event"
    ERROR = "error"
    TERMINAL = "terminal"
    DONE = "done"


class AgentEvent(BaseModel):
    type: str
    task_id: str
    step: int = 0
    message: str | None = None
    elapsed_ms: int | None = None
    content: str | None = None
    reasoning_content: str | None = None
    event: dict[str, Any] | None = None
    code: str | None = None
    outcome: str | None = None
    reason: str | None = None
    tool_calls: int | None = None
    duration_ms: int | None = None

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type, "task_id": self.task_id}
        if self.step is not None:
            payload["step"] = self.step
        if self.message is not None:
            payload["message"] = self.message
        if self.elapsed_ms is not None:
            payload["elapsed_ms"] = self.elapsed_ms
        if self.content is not None:
            payload["content"] = self.content
        if self.reasoning_content is not None:
            payload["reasoning_content"] = self.reasoning_content
        if self.event is not None:
            payload["event"] = self.event
        if self.code is not None:
            payload["code"] = self.code
        if self.outcome is not None:
            payload["outcome"] = self.outcome
        if self.reason is not None:
            payload["reason"] = self.reason
        if self.tool_calls is not None:
            payload["tool_calls"] = self.tool_calls
        if self.duration_ms is not None:
            payload["duration_ms"] = self.duration_ms
        return payload


class AgentSink:
    async def send(self, event: AgentEvent) -> None:
        raise NotImplementedError

    async def enqueue(self, text: str) -> None:
        raise NotImplementedError


class ToolOutput(BaseModel):
    status: ToolStatus
    result: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    content: str = ""
    calls: list[ToolCall] = Field(default_factory=list)
    finish: str = ""
    usage: dict[str, Any] | None = None


class RuntimeConfig(BaseModel):
    orca_key: str
    insta_key: str | None = None
    browser_hosts: set[str] = Field(default_factory=set)
    allowed_domains: list[str] = Field(default_factory=list)
    nixpkgs_rev: str = ""
    nix_version: str = ""
    nix_installer_sha256: str = ""
    deployment_max_request_ms: int = 0
