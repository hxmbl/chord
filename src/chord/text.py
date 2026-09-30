"""Getting untrusted text into the log as exactly one plain line.

The log is the thing someone reads when a hand-over went wrong, so a line that
somebody else wrote — an issue title, a comment, an error message from Linear —
has to stay one line, and must not be able to carry terminal escapes that
redraw the screen or imitate another entry.
"""

import re
import unicodedata

# Control characters and invisible format characters — the things that can
# carry a terminal escape, a zero-width trick, or a fake line break.
#
# Ordinary whitespace is deliberately *not* in this pattern. It is left alone
# and folded by the caller's `split()`, because deleting it would weld the words
# on either side together ("a\x07b" becoming "ab"), which changes what the text
# says. These become a space instead, so they act as a word boundary.
_UNSAFE = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u200b-\u200f\u2028\u2029\ufeff]"
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
