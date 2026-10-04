"""Getting untrusted text into the log as exactly one plain line.

The log is the thing someone reads when a hand-over went wrong, so a line that
somebody else wrote — an issue title, a comment, an error message from Linear —
has to stay one line, and must not be able to carry terminal escapes that
redraw the screen or imitate another entry.
"""

import re
import unicodedata

# Control characters and invisible format characters — the things that can
# carry a terminal escape, a zero-width trick, a fake line break, or reversed
# text.
#
# Ordinary whitespace is deliberately *not* in this pattern. It is left alone
# and folded by the caller's `split()`, because deleting it would weld the words
# on either side together ("a\x07b" becoming "ab"), which changes what the text
# says. These become a space instead, so they act as a word boundary.
#
# The bidirectional formatting block was the one gap. Everything else this
# pattern covers is a control character a terminal will *execute* — an escape
# sequence that repaints the screen, a control code that moves the cursor — and
# that was already handled. U+202E was not: an issue title carrying a
# right-to-left override reached the log intact, and everything after it on that
# line rendered reversed. It cannot run anything, but it can make a log entry
# read as the opposite of what it says, in the file a person opens to find out
# what happened. The whole block is covered rather than just U+202E, because the
# others do the same thing in a slightly different way.
_UNSAFE = re.compile(
    # Written as escapes rather than literals on purpose. Listing these
    # characters as themselves puts them in a character class beside a
    # range operator, where an accidental leading "-" silently becomes the
    # start of a range and eats ordinary punctuation. That happened here: a
    # hyphen-minus in the class turned "-" and U+200F into a range, so
    # "ENG-1" came out of the log as "ENG 1". Every code point below is
    # spelled out, so there is nothing for a range to swallow.
    "[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f"
    "\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069"
    "\u2028\u2029\ufeff]"
)

LIMIT = 200


def one_line(value: object, limit: int = LIMIT) -> str:
    """Flatten `value` into one line of safe, printable text.

    Called at the log boundary rather than at each producer, so a new source of
    untrusted text is covered by default instead of by remembering.
    """
    text = value if isinstance(value, str) else str(value)
    text = unicodedata.normalize("NFKC", text)
    text = _UNSAFE.sub(" ", text)
    # split() on default whitespace folds newlines, tabs, runs of spaces and
    # the likes into single spaces, so the result is one line by construction.
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text
