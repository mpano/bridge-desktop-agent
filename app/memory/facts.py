"""Things the user asked Bridge to remember ("Olivier is my brother"), on this Mac.

Memories are short facts, shown to the model with every request so it can fill in
details. They are never instructions and never secrets: passwords, keys and card or
account numbers are refused.
"""

from __future__ import annotations

import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

MAX_FACTS = 200
MAX_LENGTH = 300
PROMPT_BUDGET = 4000  # Characters of memories included with each request.

SECRETS = [
    re.compile(
        r"\b(password|passcode|passwd|pin code|pin is|security code|cvv|api key|secret)\b", re.I
    ),
    re.compile(r"\b(sk|pk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(ghp|gho|xox[abp]|AKIA)[A-Za-z0-9_-]{10,}"),
    re.compile(r"\b(?:\d[ -]?){13,19}\b"),  # Card or account numbers.
]


@dataclass(frozen=True)
class Fact:
    id: int
    text: str
    created_at: float


class FactStore:
    def __init__(self, path: Path | str):
        self.path = str(path)
        with sqlite3.connect(self.path) as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS memories (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "text TEXT NOT NULL, created_at REAL NOT NULL)"
            )

    def list(self) -> list[Fact]:
        with sqlite3.connect(self.path) as db:
            rows = db.execute("SELECT id, text, created_at FROM memories ORDER BY id").fetchall()
        return [Fact(*row) for row in rows]

    def add(self, text: str) -> Fact:
        text = " ".join(text.split()).strip()
        if not text:
            raise ValueError("There's nothing to remember.")
        if len(text) > MAX_LENGTH:
            raise ValueError(f"Keep memories under {MAX_LENGTH} characters.")
        if any(pattern.search(text) for pattern in SECRETS):
            raise ValueError(
                "I don't store passwords, codes, keys or card numbers. Keep those in a "
                "password manager."
            )
        existing = self.list()
        for fact in existing:
            if fact.text.casefold() == text.casefold():
                return fact
        if len(existing) >= MAX_FACTS:
            raise ValueError("Memory is full. Forget some things first.")
        with sqlite3.connect(self.path) as db:
            cursor = db.execute(
                "INSERT INTO memories (text, created_at) VALUES (?, ?)", (text, time.time())
            )
            new_id = cursor.lastrowid
        return Fact(new_id, text, time.time())

    def forget(self, fact_id: int) -> Fact | None:
        fact = next((item for item in self.list() if item.id == fact_id), None)
        if fact is not None:
            with sqlite3.connect(self.path) as db:
                db.execute("DELETE FROM memories WHERE id = ?", (fact_id,))
        return fact

    def forget_matching(self, words: str) -> Fact:
        wanted = [word for word in words.casefold().split() if word]
        matches = [
            fact for fact in self.list() if all(word in fact.text.casefold() for word in wanted)
        ]
        if not matches:
            raise ValueError(f"I don't remember anything about “{words}”.")
        if len(matches) > 1:
            options = "; ".join(f"“{fact.text}”" for fact in matches[:5])
            raise ValueError(f"Several memories match: {options}. Be more specific.")
        return self.forget(matches[0].id)

    def prompt_block(self) -> str:
        """The newest memories that fit the budget, oldest first, for the system prompt."""
        lines, used = [], 0
        for fact in reversed(self.list()):
            line = f"- {fact.text}"
            if used + len(line) > PROMPT_BUDGET:
                break
            lines.append(line)
            used += len(line) + 1
        return "\n".join(reversed(lines))
