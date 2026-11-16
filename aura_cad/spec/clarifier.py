"""Pure slot-filling state machine with bounded, single-question turns."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from aura_cad.config import MAX_QUESTIONS
from aura_cad.models import CadSpec, Slot
from aura_cad.spec.intent import detect_intent
from aura_cad.spec.templates import default_description, slots_for

_SKIP = re.compile(r"\b(?:just build it|you decide|surprise me|no preference|skip)\b", re.I)
_CANCEL = re.compile(r"\b(?:cancel|never mind|nevermind|stop cad)\b", re.I)
_YES = re.compile(r"\b(?:yes|yeah|yep|build it|go ahead|do it|confirm)\b", re.I)
_NO = re.compile(r"\b(?:no|cancel|not now|don't build|do not build)\b", re.I)
_CHANGE = re.compile(r"\b(?:change|edit|adjust) details\b", re.I)
_ORDINALS = ("first", "second", "third", "fourth", "fifth")
_NUMBER_WORDS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}


class StepKind(str, Enum):
    QUESTION = "question"
    CONFIRM = "confirm"
    BUILD = "build"
    CANCEL = "cancel"
    NONE = "none"


@dataclass(frozen=True)
class ClarificationStep:
    kind: StepKind
    message: str = ""
    question: str = ""
    options: tuple[str, ...] = ()
    spec: CadSpec | None = None


class CadClarifier:
    """Hold only pure conversation state; UI and model calls stay outside."""

    def __init__(self, max_questions: int = MAX_QUESTIONS) -> None:
        self.max_questions = max(1, int(max_questions))
        self.spec: CadSpec | None = None
        self._slots: tuple[Slot, ...] = ()
        self._asked = 0
        self._awaiting_confirmation = False

    @property
    def active(self) -> bool:
        return self.spec is not None

    def start(self, text: str) -> ClarificationStep:
        object_type = detect_intent(text)
        if object_type is None:
            return ClarificationStep(StepKind.NONE)
        self.spec = CadSpec(object_type, default_description(object_type))
        self._slots = slots_for(object_type)
        self._asked = 0
        self._awaiting_confirmation = False
        self._apply_answers(text, None)
        return self._next_step()

    def answer(self, text: str) -> ClarificationStep:
        if self.spec is None:
            return ClarificationStep(StepKind.NONE)
        answer = " ".join(str(text or "").split())
        if _CANCEL.search(answer):
            return self._finish(StepKind.CANCEL, "CAD request cancelled.")
        if self._awaiting_confirmation:
            choice = self._spoken_choice_index(answer, 3)
            if choice == 0:
                answer = "yes"
            elif choice == 1:
                answer = "change details"
            elif choice == 2:
                answer = "cancel"
            if _YES.search(answer):
                spec = self.spec
                self._reset()
                return ClarificationStep(StepKind.BUILD, "Building the model.", spec=spec)
            if _NO.search(answer):
                return self._finish(StepKind.CANCEL, "Model build cancelled.")
            if _CHANGE.search(answer):
                first = self._slots[0]
                self.spec.slots.pop(first.key, None)
                self._awaiting_confirmation = False
                self._asked = max(0, self._asked - 1)
                return self._next_step()
            if answer:
                self._awaiting_confirmation = False
                self._apply_answers(answer, None)
                return self._next_step()
        if _SKIP.search(answer):
            self._fill_defaults()
        else:
            current = self._current_missing()
            choice = self._spoken_choice_index(
                answer, len(current.options) if current is not None else 0
            )
            if choice is not None and current is not None:
                answer = current.options[choice]
            self._apply_answers(answer, current)
        return self._next_step()

    @staticmethod
    def _spoken_choice_index(answer: str, option_count: int) -> int | None:
        """Resolve spoken ordinals and explicit option numbers to zero-based choices."""
        if option_count <= 0:
            return None
        words = "|".join(_ORDINALS[:option_count])
        match = re.search(
            rf"\b(?:{words})(?:\s+(?:option|choice|one))?\b|"
            rf"\b(?:option|choice)\s+([1-{min(option_count, 5)}])\b|"
            rf"\b(1st|2nd|3rd|4th|5th)\b",
            answer,
            re.I,
        )
        if not match:
            return None
        token = match.group(0).casefold()
        if match.group(1):
            return int(match.group(1)) - 1
        ordinal = re.search(r"\d+", token)
        if ordinal:
            return int(ordinal.group()) - 1
        return _ORDINALS.index(token.split()[0])

    def _next_step(self) -> ClarificationStep:
        if self.spec is None:
            return ClarificationStep(StepKind.NONE)
        missing = self._current_missing()
        if missing is not None and self._asked < self.max_questions:
            self._asked += 1
            return ClarificationStep(
                StepKind.QUESTION,
                question=missing.question,
                options=missing.options,
                spec=self.spec,
            )
        self._fill_defaults()
        self._awaiting_confirmation = True
        summary = self._summary()
        return ClarificationStep(
            StepKind.CONFIRM,
            message=f"{summary}. Build it?",
            question=f"{summary}. Build it?",
            options=("Build it", "Change details", "Cancel"),
            spec=self.spec,
        )

    def _current_missing(self) -> Slot | None:
        if self.spec is None:
            return None
        return next((slot for slot in self._slots if slot.key not in self.spec.slots), None)

    def _apply_answers(self, text: str, current: Slot | None) -> None:
        if self.spec is None:
            return
        lowered = text.casefold()
        for slot in self._slots:
            if slot.key in self.spec.slots:
                continue
            value = self._extract(slot, lowered)
            if value is not None:
                self.spec.slots[slot.key] = value
        if current is not None and current.key not in self.spec.slots:
            value = self._normalize(current, text)
            if value is not None:
                self.spec.slots[current.key] = value

    @staticmethod
    def _extract(slot: Slot, text: str):
        if slot.key == "cylinders":
            match = re.search(
                r"\b(\d{1,2}|two|three|four|five|six|seven|eight|nine|ten|"
                r"eleven|twelve)\s*[- ]?cylinders?\b",
                text,
            )
            if match:
                number = CadClarifier._number_value(match.group(1))
                return number if number is not None and 1 <= number <= 12 else None
        if slot.key == "teeth":
            match = re.search(r"\b(\d{1,3})\s*(?:teeth|tooth)?\b", text)
            if match:
                number = int(match.group(1))
                return number if 6 <= number <= 120 else None
        if slot.key == "module_mm":
            match = re.search(r"\bmodule\s*(?:of\s*)?(\d+(?:\.\d+)?)", text)
            if match:
                number = float(match.group(1))
                return number if 0.2 <= number <= 10 else None
        if slot.key in {"cylinders", "holes", "storeys", "width_mm", "thickness_mm"}:
            match = re.search(r"\b(\d{1,4})\b", text)
            if match:
                number = int(match.group(1))
                return number if number > 0 else None
        for option in slot.options:
            if re.search(r"\b" + re.escape(option.casefold()) + r"\b", text):
                if slot.key == "layout" and option.casefold() == "v-type":
                    engine = re.search(r"\bv\s*([468])\b", text)
                    return f"V{engine.group(1)}" if engine else "V8"
                return option
        if slot.key == "layout":
            engine = re.search(r"\bv\s*([468])\b", text)
            if engine:
                return f"V{engine.group(1)}"
            if "inline" in text:
                return "Inline"
            if "flat" in text or "boxer" in text:
                return "Flat"
        if slot.key == "detail":
            if "full" in text or "internals" in text:
                return "Internals"
            if "simple" in text or "exterior" in text:
                return "Exterior"
        if slot.key == "ventilation":
            if re.search(r"\b(?:yes|add|with)\b", text):
                return "Yes"
            if re.search(r"\b(?:no|without)\b", text):
                return "No"
        return None

    @staticmethod
    def _normalize(slot: Slot, text: str):
        answer = text.strip()
        if not answer:
            return None
        if slot.options:
            for option in slot.options:
                if answer.casefold() == option.casefold() or option.casefold() in answer.casefold():
                    if slot.key == "layout" and option.casefold() == "v-type":
                        engine = re.search(r"\bv\s*([468])\b", answer, re.I)
                        return f"V{engine.group(1)}" if engine else "V8"
                    return option
        if slot.key == "teeth":
            match = re.search(r"\d{1,3}", answer)
            if match and 6 <= int(match.group()) <= 120:
                return int(match.group())
        if slot.key == "module_mm":
            match = re.search(r"\d+(?:\.\d+)?", answer)
            if match and 0.2 <= float(match.group()) <= 10:
                return float(match.group())
        if slot.key == "cylinders":
            match = re.search(
                r"\b(\d{1,2}|two|three|four|five|six|seven|eight|nine|ten|"
                r"eleven|twelve)\b",
                answer.casefold(),
            )
            if match:
                number = CadClarifier._number_value(match.group(1))
                if number is not None and 1 <= number <= 12:
                    return number
        if slot.key in {"cylinders", "holes", "storeys", "width_mm", "thickness_mm"}:
            match = re.search(r"\d{1,4}", answer)
            if match and int(match.group()) > 0:
                return int(match.group())
        if slot.key == "purpose":
            return answer[:80]
        return None

    @staticmethod
    def _number_value(token: str) -> int | None:
        return int(token) if token.isdigit() else _NUMBER_WORDS.get(token)

    def _fill_defaults(self) -> None:
        if self.spec is None:
            return
        for slot in self._slots:
            if slot.key not in self.spec.slots:
                self.spec.slots[slot.key] = slot.default
                self.spec.assumptions.append(f"{slot.label}: {slot.default}")

    def _summary(self) -> str:
        if self.spec is None:
            return "CAD model"
        values = [self.spec.object_type.replace("_", " ")]
        for slot in self._slots:
            value = self.spec.slots.get(slot.key)
            if value is not None:
                values.append(f"{value} {slot.label}")
        if self.spec.assumptions:
            values.append("defaults used")
        return ", ".join(values)

    def _finish(self, kind: StepKind, message: str) -> ClarificationStep:
        spec = self.spec
        self._reset()
        return ClarificationStep(kind, message=message, spec=spec)

    def _reset(self) -> None:
        self.spec = None
        self._slots = ()
        self._asked = 0
        self._awaiting_confirmation = False
