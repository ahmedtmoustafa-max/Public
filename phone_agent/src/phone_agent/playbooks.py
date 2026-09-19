"""Saved key sequences for numbers you call often.

The first time you call somewhere, `auto` mode works the menu out. When that
works, the sequence is written back here so the next call can run in `script`
mode -- faster, cheaper, and it needs no transcription at all.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

PLAYBOOK_DIR = Path("config/playbooks")


@dataclass
class Playbook:
    id: str
    label: str = ""
    number: str = ""
    keys: str = ""
    goal: str = "Reach a live human agent."
    notes: str = ""
    learned: bool = False
    source_calls: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "number": self.number,
            "keys": self.keys,
            "goal": self.goal,
            "notes": self.notes,
            "learned": self.learned,
            "source_calls": self.source_calls,
        }


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug or "unnamed"


class PlaybookStore:
    def __init__(self, directory: Path | str = PLAYBOOK_DIR):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)

    def all(self) -> list[Playbook]:
        books: list[Playbook] = []
        for path in sorted(self.directory.glob("*.yaml")):
            book = self._read(path)
            if book:
                books.append(book)
        return books

    def _read(self, path: Path) -> Playbook | None:
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except (OSError, yaml.YAMLError) as exc:
            log.warning("skipping playbook %s: %s", path, exc)
            return None
        if not isinstance(data, dict):
            return None
        data.setdefault("id", path.stem)
        known = {f for f in Playbook.__dataclass_fields__}
        return Playbook(**{k: v for k, v in data.items() if k in known})

    def get(self, playbook_id: str) -> Playbook | None:
        path = self.directory / f"{_slug(playbook_id)}.yaml"
        return self._read(path) if path.exists() else None

    def find_for_number(self, number: str) -> Playbook | None:
        digits = re.sub(r"\D", "", number or "")
        if not digits:
            return None
        for book in self.all():
            if re.sub(r"\D", "", book.number or "") == digits:
                return book
        return None

    def save(self, book: Playbook) -> Path:
        book.id = _slug(book.id)
        path = self.directory / f"{book.id}.yaml"
        path.write_text(yaml.safe_dump(book.as_dict(), sort_keys=False))
        log.info("saved playbook %s", path)
        return path

    def learn(
        self, *, label: str, number: str, keys: str, goal: str, call_id: str
    ) -> Playbook | None:
        """Record the key sequence that got a call through to a person."""
        if not keys:
            return None
        existing = self.find_for_number(number)
        if existing and existing.keys == keys:
            if call_id not in existing.source_calls:
                existing.source_calls.append(call_id)
                self.save(existing)
            return existing
        book = Playbook(
            id=_slug(label or number),
            label=label,
            number=number,
            keys=keys,
            goal=goal,
            notes="Learned automatically from a call that reached a person.",
            learned=True,
            source_calls=[call_id],
        )
        self.save(book)
        return book


def parse_key_script(keys: str) -> list[str]:
    """Split '1,w3,0' into ['1', 'w3', '0'] -- one step per menu level."""
    steps = [step.strip() for step in (keys or "").split(",")]
    return [re.sub(r"[^0-9*#w]", "", step) for step in steps if step.strip()]
