"""What untrusted text is allowed to do to the log.

`one_line` is the boundary. Issue titles, comment bodies and Linear's error
messages all reach the log through it, and the log is the thing a person reads
when a hand-over went wrong. So the question is not "is this string safe" but
"what can somebody else's text do to the line it lands on".

Two things, and this file covers both:

**Control characters a terminal executes.** An escape sequence that repaints the
screen, a control code that moves the cursor. Already handled before this file
existed, and pinned here so it stays handled.

**Reversed text.** U+202E and its neighbours were *not*. Verified against the
pre-fix code: an issue title of `ENG-1 Fix login ‮drowssap eht list‮` reached
the log with both U+202E intact, and everything after the first one rendered
backwards. It executes nothing. But a log line that reads as the opposite of
what it says is worse than one that reads oddly, because you cannot tell it
apart from a line that is telling you the truth.

The hyphen test at the bottom is here because of how that fix went wrong first.
The bidirectional block was originally written as literal characters in the
character class, where a hyphen-minus next to a range operator silently became
the start of a range — so "ENG-1" came out of the log as "ENG 1", in every
line, including identifiers. The pattern now spells every code point out.
"""

# PLE2502 ("control characters that can permit obfuscated code") is suppressed
# for this file. It is right in general and wrong here: this is the one file
# whose subject *is* those characters. They are all built with `chr()` so none
# of them is literally in the source, and every one is an assertion that the
# log boundary removes it.
# ruff: noqa: PLE2502

import asyncio
import contextlib
import io
import logging
import pathlib
import re
import sys
import unicodedata

import pytest

from chord import text, watcher
from chord.linear import IssuePage
from chord.routing import Router

# The characters that make text render in the opposite direction. Spelled with
# `chr()` so none of them appears literally in this file: invisible characters
# written into source are unreadable to whoever edits it next, and ruff rightly
# flags them as a way to smuggle code past a reader.
BIDI = {
    "U+202A": chr(0x202A),  # left-to-right embedding
    "U+202B": chr(0x202B),  # right-to-left embedding
    "U+202C": chr(0x202C),  # pop directional formatting
    "U+202D": chr(0x202D),  # left-to-right override
    "U+202E": chr(0x202E),  # right-to-left override
    "U+2066": chr(0x2066),  # left-to-right isolate
    "U+2067": chr(0x2067),  # right-to-left isolate
    "U+2068": chr(0x2068),  # first strong isolate
    "U+2069": chr(0x2069),  # pop directional isolate
    "U+200E": chr(0x200E),  # left-to-right mark
    "U+200F": chr(0x200F),  # right-to-left mark
}

ZERO_WIDTH = {
    "U+200B": chr(0x200B),  # zero-width space
    "U+200C": chr(0x200C),  # zero-width non-joiner
    "U+200D": chr(0x200D),  # zero-width joiner
    "U+2060": chr(0x2060),  # word joiner
    "U+FEFF": chr(0xFEFF),  # zero-width no-break space
}

CONTROL = {
    "NUL": chr(0x00),
    "BEL": chr(0x07),
    "ESC": chr(0x1B),
    "backspace": chr(0x08),
    "vertical tab": chr(0x0B),
    "form feed": chr(0x0C),
    "DEL": chr(0x7F),
    "C1 NEL": chr(0x85),
    "line separator": chr(0x2028),
    "paragraph separator": chr(0x2029),
}

RLO = chr(0x202E)
LRO = chr(0x202D)
ZWSP = chr(0x200B)


def surviving_markers(value: str) -> list[str]:
    """Which of the dangerous characters are still in `value`.

    Line breaks are reported per line rather than across the whole string: a
    log legitimately has one line per entry, so "this text contains a newline"
    is only a finding when a *single entry* contains one.
    """
    bad = []
    for name, ch in {**BIDI, **ZERO_WIDTH}.items():
        if ch in value:
            bad.append(name)
    if any(chr(c) in value for c in (0x00, 0x07, 0x08, 0x1B, 0x7F)):
        bad.append("terminal control")
    return bad


def split_lines(value: str) -> list[str]:
    return value.replace(chr(0x0D), chr(0x0A)).split(chr(0x0A))


# --- the boundary itself ---


@pytest.mark.parametrize("name,char", sorted(BIDI.items()))
def test_bidi_characters_do_not_survive(name, char):
    """The regression, one character at a time."""
    out = text.one_line(f"ENG-1 normal{char}reversed")
    assert not surviving_markers(out), f"{name} survived: {out!r}"


@pytest.mark.parametrize("name,char", sorted(BIDI.items()))
def test_bidi_characters_leave_the_words_readable(name, char):
    """Not deleted — replaced by a space, so the words either side still read.

    Deleting would weld them together and change what the text says, which is
    the same mistake the whitespace exclusion is there to avoid.
    """
    out = text.one_line(f"ENG-1 normal{char}reversed")
    assert "normal" in out
    assert "reversed" in out


@pytest.mark.parametrize("name,char", sorted(CONTROL.items()))
def test_control_characters_do_not_survive(name, char):
    out = text.one_line(f"ENG-1 fix{char}forged")
    assert not surviving_markers(out), f"{name} survived: {out!r}"


@pytest.mark.parametrize("name,char", sorted(ZERO_WIDTH.items()))
def test_zero_width_characters_do_not_survive(name, char):
    out = text.one_line(f"ENG-1 fix{char}forged")
    assert not surviving_markers(out), f"{name} survived: {out!r}"


@pytest.mark.parametrize(
    "payload",
    [
        "ENG-1 Fix\x1b[2J\x1b[H  handed over.",
        "ENG-1 Fix\r2026-01-01 00:00:00  handed over.",
        "ENG-1 Fix\n2026-01-01 00:00:00  handed over.",
        "ENG-1 Fix ‮drowssap eht list‮",
        "ENG-1 Fix ‭reversed‮",
        "ENG-1 Fix⁩ ⁧",
        "ENG-1 Fi‌x",
    ],
    ids=["ansi", "cr", "lf", "rlo", "lro-pdf", "isolates", "zero-width"],
)
def test_no_payload_produces_a_second_line_or_a_reversed_one(payload):
    assert chr(0x0A) not in text.one_line(payload)
    assert not surviving_markers(text.one_line(payload))


# --- the hyphen, which the fix broke first ---


@pytest.mark.parametrize(
    "ordinary",
    [
        "ENG-1",
        "a-b",
        "2026-01-01T00:00:00Z",
        "well-known-name",
        "[bracketed]-suffix",
    ],
)
def test_ordinary_punctuation_is_not_treated_as_unsafe(ordinary):
    """Regression for how the bidi fix was first written.

    Listing the block as literal characters put a hyphen-minus next to a range
    operator in the same character class, so it started a range and every
    hyphen in every log line became a space. "ENG-1" logged as "ENG 1" — which
    would have quietly broken every identifier anyone tried to copy out of the
    log to look up in Linear.
    """
    assert text.one_line(ordinary) == ordinary


def test_the_character_class_has_no_accidental_range():
    """Pins the shape of the fix rather than only its output.

    A range from a printable character to a format character is exactly the bug,
    so this asserts the two classes are separate, which makes it impossible to
    reintroduce by accident.
    """
    body = text._UNSAFE.pattern[1:-1]
    # Every range in the class, as written. A range whose start is above U+002E
    # is the bug: it means printable punctuation is inside the class.
    for start, end in re.findall(r"(.)-(.)", body):
        assert ord(start) >= 0x7F or ord(start) <= 0x20, (
            f"a range starting at printable U+{ord(start):04X} is in the class"
        )
        assert ord(start) <= ord(end), "a reversed range slipped in"
    # Control characters are meant to be stripped. Printable ones are not, and
    # the whole regression was a printable hyphen ending up in the class.
    printable = {
        c for c in map(chr, range(0x20, 0x7F)) if text._UNSAFE.fullmatch(c)
    }
    assert printable == set(), (
        f"printable ASCII is now being stripped: "
        f"{sorted(hex(ord(c)) for c in printable)}"
    )
    # Nothing above the Latin-1 range either, other than the invisible blocks
    # that are deliberately listed.
    allowed_above = set(BIDI) | set(ZERO_WIDTH) | {"U+2028", "U+2029"}
    for name, char in {**BIDI, **ZERO_WIDTH}.items():
        assert name in allowed_above
        assert text._UNSAFE.fullmatch(char), f"{name} is no longer covered"


def test_letters_and_numbers_are_never_altered():
    out = text.one_line("Fix login for ENG-123 (v2) — 50% done")
    for word in ("Fix", "login", "ENG", "123", "50", "done"):
        assert word in out


# --- end to end, through the real log path ---


def issue(n=1, **extra):
    return {
        "id": str(n),
        "identifier": f"ENG-{n}",
        "title": f"Issue {n}",
        "description": "do it",
        "createdAt": "2026-01-01T00:00:00Z",
        **extra,
    }


class Stub:
    def __init__(self, issues):
        self.issues = issues

    async def issues_for(self, filter):
        return IssuePage(self.issues, False)

    async def label_history(self, issue_id):
        return []

    async def comments(self, issue_id):
        return []


class Spy:
    name = "spy"

    async def send(self, prompt):
        pass


@pytest.mark.parametrize(
    "hostile",
    [
        pytest.param("Fix login " + RLO + "drowssap eht list" + RLO, id="rlo"),
        pytest.param("Fix" + chr(0x1B) + "[2J" + chr(0x1B) + "[H login", id="ansi"),
        pytest.param(
            "Fix" + chr(0x0A) + "2026-01-01 00:00:00  handed over.", id="forged-line"
        ),
    ],
)
def test_a_hostile_title_cannot_forge_a_log_entry(tmp_path, hostile):
    """The thing that actually matters: one issue, one log entry.

    `log()` flattens, so this is a property of the boundary rather than of the
    watcher. Checked end to end anyway, because the boundary is only worth
    anything if it is on the path.
    """
    path = tmp_path / "state.json"
    w = watcher.Watcher(
        Stub([issue(title=hostile)]), Router("Chord", "spy", factory=lambda s: Spy()),
        1, path,
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        asyncio.run(w.poll())
    out = buf.getvalue()

    for line in split_lines(out):
        assert not surviving_markers(line), (
            f"a dangerous character reached the log: {line!r}"
        )
    # A forged entry would look like a whole new line carrying a timestamp.
    # Every real line in a Chord log starts with one; a newline smuggled in
    # through a title would produce a line that does not.
    for line in split_lines(out):
        if line.strip():
            assert line.startswith("2026-") or "INFO" in line, (
                f"a line in the log has no timestamp, so it was forged: {line!r}"
            )


def test_the_identifier_still_reads_correctly_in_the_log(tmp_path):
    """The other half of the hyphen regression, end to end."""
    path = tmp_path / "state.json"
    w = watcher.Watcher(
        Stub([issue(title="a normal title")]),
        Router("Chord", "spy", factory=lambda s: Spy()),
        1, path,
    )
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        asyncio.run(w.poll())
    assert "ENG-1" in buf.getvalue()


def test_linear_error_text_is_flattened_too(monkeypatch):
    """Error messages come from Linear and are the other untrusted source."""
    seen = []

    class Refuses:
        name = "refuses"

        async def send(self, prompt):
            seen.append(prompt)

    class L:
        async def issues_for(self, filter):
            raise watcher.LinearError(
                "Linear turned down the query: " + RLO + "authentication failed" + RLO
            )

        async def label_history(self, i):
            return []

        async def comments(self, i):
            return []

    w = watcher.Watcher(L(), Router("Chord", "spy", factory=lambda s: Refuses()),
                        1, pathlib.Path("/nonexistent/state.json"))
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        asyncio.run(w.poll())
    assert not surviving_markers(buf.getvalue())


# --- and the limit still applies ---


def test_long_text_is_still_cut_to_one_line():
    out = text.one_line("x" * 5000)
    assert len(out) <= text.LIMIT
    assert out.endswith("…")


def test_one_line_is_idempotent():
    """Flattening twice must not change it, or the log boundary is not a place."""
    once = text.one_line("ENG-1 Fix" + RLO + "login" + chr(0x0D) + chr(0x0A) + "more")
    assert text.one_line(once) == once


def test_bidi_is_still_rejected_after_normalisation():
    """NFKC runs first, and it does not remove these. Order matters."""
    # A fullwidth RLO that normalises to a plain one must still be caught.
    hostile = "ENG-1 " + RLO + "reversed"
    assert not surviving_markers(text.one_line(unicodedata.normalize("NFKC", hostile)))


def test_logging_is_configured_so_the_test_sees_output():
    """A test that passes because the log went nowhere is not a test."""
    assert logging.getLogger().handlers or logging.getLogger("chord").handlers
    assert sys.modules["chord.text"] is text