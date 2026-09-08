"""Synthetic source for the offline Poetry Rhythm Trainer demo."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RhythmQuestion:
    question_id: str
    poem_line: str
    reference: str
    hint: str


QUESTION_BANK = (
    RhythmQuestion("Q01", "白日依山尽", "白日/依山尽", "先读景物，再读动作。"),
    RhythmQuestion("Q02", "春风花草香", "春风/花草香", "前两个字和后三个字分开。"),
    RhythmQuestion("Q03", "明月松间照", "明月/松间照", "先读主体，再读地点和动作。"),
    RhythmQuestion("Q04", "江清月近人", "江清/月近人", "前半句写江水，后半句写月与人。"),
)


def normalize_rhythm(value: str) -> str:
    cleaned = value.strip().replace("｜", "/").replace("|", "/")
    return "/".join(part.strip() for part in cleaned.split("/") if part.strip())


def find_question(question_id: str) -> RhythmQuestion:
    for question in QUESTION_BANK:
        if question.question_id == question_id:
            return question
    raise KeyError(f"unknown question: {question_id}")


def check_answer(question_id: str, answer: str) -> dict[str, object]:
    question = find_question(question_id)
    normalized = normalize_rhythm(answer)
    correct = normalized == question.reference
    return {
        "question_id": question_id,
        "correct": correct,
        "reference": question.reference,
        "feedback": "节奏划分正确。" if correct else f"可以参考：{question.hint}",
    }


def build_practice(question_ids: tuple[str, ...]) -> list[dict[str, str]]:
    practice = []
    for question_id in question_ids:
        question = find_question(question_id)
        practice.append(
            {
                "question_id": question.question_id,
                "poem_line": question.poem_line,
                "prompt": "请用 / 标出建议停顿位置",
            }
        )
    return practice


def score_session(answers: dict[str, str]) -> dict[str, object]:
    details = [check_answer(question_id, answer) for question_id, answer in answers.items()]
    correct_count = sum(1 for detail in details if detail["correct"])
    total = len(details)
    return {
        "answered": total,
        "correct": correct_count,
        "accuracy": round(correct_count / total * 100, 1) if total else 0.0,
        "details": details,
    }


def demo_snapshot() -> dict[str, object]:
    """Return fixed fictional output for the static preview."""
    answers = {
        "Q01": "白日/依山尽",
        "Q02": "春风花/草香",
        "Q03": "明月/松间照",
    }
    return score_session(answers)
