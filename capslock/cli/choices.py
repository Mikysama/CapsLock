"""Shared choice and structured-question view models for terminal frontends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Sequence


@dataclass(frozen=True)
class ChoiceOption:
    value: str
    label: str
    detail: str = ""

    @property
    def searchable_text(self) -> str:
        return f"{self.label} {self.detail} {self.value}".casefold()


@dataclass(frozen=True)
class ChoiceViewModel:
    title: str
    options: tuple[ChoiceOption, ...]
    current: str | None = None

    @classmethod
    def from_choices(
        cls,
        title: str,
        choices: Sequence[Any],
        *,
        current: str | None = None,
    ) -> "ChoiceViewModel":
        options = tuple(
            ChoiceOption(str(item.value), str(item.label), str(item.detail or ""))
            for item in choices
        )
        selected = current or next(
            (
                option.value
                for option, item in zip(options, choices, strict=True)
                if bool(getattr(item, "selected", False))
            ),
            None,
        )
        if selected not in {item.value for item in options}:
            selected = options[0].value if options else None
        return cls(title, options, selected)

    @property
    def filterable(self) -> bool:
        return len(self.options) > 8

    def filtered(self, query: str) -> tuple[ChoiceOption, ...]:
        terms = query.casefold().split()
        if not terms:
            return self.options
        return tuple(
            option
            for option in self.options
            if all(term in option.searchable_text for term in terms)
        )


@dataclass(frozen=True)
class QuestionViewModel:
    identifier: str
    question: str
    options: tuple[ChoiceOption, ...]
    multiple: bool = False
    allow_free_text: bool = True


def questions_view_model(
    raw_questions: Iterable[object],
) -> tuple[QuestionViewModel, ...]:
    result: list[QuestionViewModel] = []
    for raw in raw_questions:
        if not isinstance(raw, dict):
            continue
        options = tuple(
            ChoiceOption(
                str(item.get("value", item.get("label", ""))),
                str(item.get("label", item.get("value", ""))),
                str(item.get("description", item.get("detail", "")) or ""),
            )
            for item in raw.get("options", [])
            if isinstance(item, dict) and (item.get("label") or item.get("value"))
        )
        result.append(
            QuestionViewModel(
                str(raw.get("id", "")),
                str(raw.get("question", "Question")),
                options,
                bool(raw.get("multiple", False)),
                bool(raw.get("allow_free_text", True)),
            )
        )
    return tuple(result)
