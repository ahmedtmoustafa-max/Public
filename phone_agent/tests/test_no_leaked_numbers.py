"""Guard against committing a real phone number.

This repository is public. A personal number in a tracked file is visible to
everyone and to every scraper, and rewriting history does not reliably remove
it. So: every phone number that appears in a tracked file must be a
documentation placeholder, which in North America means one containing the
reserved 555 exchange. Real numbers belong in .env, which is gitignored.

If this test fails, do not add your number to the allowlist -- move it to .env.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]

PATTERNS = [
    re.compile(r"\+\d{8,15}"),                                     # E.164
    re.compile(r"\+1[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}"),  # +1 (519) 555-1234
    re.compile(r"\b\d{3}[.\s-]\d{3}[.\s-]\d{4}\b"),                # 519-555-1234
]

#: This file necessarily contains number-shaped literals (the patterns above
#: and the cases below), so scanning it verbatim would always fail. It is not
#: exempted, though -- `_scannable` strips only the lines that define the
#: patterns, leaving the test cases themselves subject to the same rule.
SELF = "tests/test_no_leaked_numbers.py"
PATTERN_MARKER = "re.compile("

#: Non-NANP placeholders that have no 555 exchange to check.
EXEMPT_LITERALS = {"+448005551212"}


def tracked_text_files() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=REPO, capture_output=True, text=True, check=True
    )
    files = []
    for line in out.stdout.splitlines():
        path = REPO / line
        if not path.is_file():
            continue
        try:
            path.read_text()
        except (UnicodeDecodeError, OSError):
            continue
        files.append(path)
    return files


def _scannable(path: Path) -> str:
    """File text, minus the regex definitions in this file itself."""
    text = path.read_text()
    if path.relative_to(REPO).as_posix() != SELF:
        return text
    return "\n".join(
        "" if PATTERN_MARKER in line else line for line in text.splitlines()
    )


def looks_like_a_placeholder(number: str) -> bool:
    if number in EXEMPT_LITERALS:
        return True
    digits = re.sub(r"\D", "", number)
    national = digits[1:] if len(digits) == 11 and digits.startswith("1") else digits
    if len(national) != 10:
        return False
    # Area code or exchange must be the reserved fictional 555.
    return national[:3] == "555" or national[3:6] == "555"


def test_git_is_available():
    assert tracked_text_files(), "no tracked files found -- is this a git checkout?"


def test_no_real_phone_numbers_in_tracked_files():
    offenders: list[str] = []
    for path in tracked_text_files():
        text = _scannable(path)
        for pattern in PATTERNS:
            for match in pattern.finditer(text):
                number = match.group(0).strip()
                if not looks_like_a_placeholder(number):
                    line = text[: match.start()].count("\n") + 1
                    offenders.append(f"{path.relative_to(REPO)}:{line}  {number}")
    assert not offenders, (
        "Real-looking phone numbers in tracked files. Move them to .env "
        "(gitignored) rather than allowlisting them:\n  " + "\n  ".join(offenders)
    )


def test_env_file_is_not_tracked():
    out = subprocess.run(
        ["git", "ls-files", ".env", "config/profile.yaml"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=True,
    )
    assert not out.stdout.strip(), (
        f"These hold personal configuration and must stay untracked: {out.stdout}"
    )


@pytest.mark.parametrize(
    "number", ["+18005551212", "+15551234567", "+1 (519) 555-1234", "519-555-1234"]
)
def test_placeholders_are_accepted(number):
    assert looks_like_a_placeholder(number)


# The negative cases have to be numbers the rule rejects -- which is exactly
# what this file forbids writing down. So they are assembled from fragments at
# run time: no complete number appears in the source, and the scan above stays
# honest about its own contents.
NON_PLACEHOLDERS = [
    "+1" + "416" + "531" + "0198",
    "+1" + "416" + "555",          # too short to be a number at all
    "-".join(("416", "531", "0198")),
]


@pytest.mark.parametrize("number", NON_PLACEHOLDERS)
def test_real_looking_numbers_are_rejected(number):
    assert not looks_like_a_placeholder(number)


def test_the_scan_would_catch_a_number_added_to_a_tracked_file(tmp_path):
    """The guard is only worth having if it actually fires."""
    leak = tmp_path / "config.py"
    leak.write_text('OWNER = "' + "+1" + "416" + "531" + "0198" + '"\n')
    found = [
        match.group(0)
        for pattern in PATTERNS
        for match in pattern.finditer(leak.read_text())
        if not looks_like_a_placeholder(match.group(0))
    ]
    assert found
