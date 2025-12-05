from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Sequence, Type

from django import forms


@dataclass(frozen=True)
class GeneratedArtifact:
    filename: str
    content: bytes
    content_type: str


@dataclass(frozen=True)
class TaskExecutionResult:
    artifacts: Sequence[GeneratedArtifact]
    archive_name: str | None = None


class TaskExecutionError(Exception):
    """Raised when a backend automation task fails for a user-facing reason."""


@dataclass(frozen=True)
class TaskDefinition:
    task_id: str
    label: str
    description: str
    form_class: Type[forms.Form]
    runner: Callable[[Dict], TaskExecutionResult]


class TaskRegistry:
    def __init__(self):
        self._registry: Dict[str, TaskDefinition] = {}

    def register(self, definition: TaskDefinition):
        if definition.task_id in self._registry:
            raise ValueError(f"Task '{definition.task_id}' is already registered.")
        self._registry[definition.task_id] = definition

    def get(self, task_id: str) -> TaskDefinition:
        if task_id not in self._registry:
            raise KeyError(f"Unknown task '{task_id}'.")
        return self._registry[task_id]

    def list(self) -> List[TaskDefinition]:
        return sorted(self._registry.values(), key=lambda d: d.label.lower())

    @property
    def default_task_id(self) -> str:
        if not self._registry:
            raise RuntimeError("No automation tasks have been registered.")
        return self.list()[0].task_id


automation_registry = TaskRegistry()
