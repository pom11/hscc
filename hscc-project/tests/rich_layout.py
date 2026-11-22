"""Normalisers for Rich-rendered CLI output asserted by the tests.

A Rich view (``make_console`` + ``Panel``) lays a row out at *render* time from the
data inside it, so a phrase far enough into a row breaks at a position the test does
not control — the width is pinned to 200 for a non-tty, so the only variable left is
how long the row's data is, and on these commands that is the ``tmp_path``. Measured
across tmp-root lengths 48..210 in ``t_036fedf8`` (and re-measured repo-wide in
``t_f2382c78``): an assert that is green at one length can be red at the next.

There are two different splits and they need two different normalisers:

* **WRAP** — Rich breaks at a word boundary and the break consumes the space, so the
  words are intact but a hard newline sits where a space was. ``_flowed`` collapses
  hard wraps and any box-drawing gutter a wrapped continuation line picked up.
* **FOLD** — the token is longer than the room left (a ``tmp_path`` always is) and
  Rich breaks it with **no** space. A word-boundary reflow cannot undo that: it puts
  a space where no space was ever printed. ``_says`` strips all whitespace, which
  recovers a fold and still matches everything ``_flowed`` matches.

Use the stricter of the two that recovers the render; the looser one hides a fold you
have not noticed. Both are *display-level* only — they say "the row does say this".

What must stay RAW:

* the row status markers (``[ok]``, ``[PROBLEM]``, ``[UNVERIFIED]``) — they are the
  row's first token, nothing can wrap or fold them, and they are what proves the row
  was rendered at all. An assert that only passes after whitespace removal has to
  keep such an anchor beside it.
* anything Rich CLIPPED. When the row's own data squeezes a table's columns, Rich
  replaces characters with an ellipsis; no normaliser may recover that, and asserting
  a folded form of it would assert words the render never printed. Such cases are
  reported, not converted.

See also ``test_doctor_command.py``, which carries the same two normalisers inline for
the doctor view.
"""

# Box-drawing and block characters: Rich's frame, and the gutter a wrapped continuation
# line is padded with. Space-substituted (not deleted) so a fold across a boundary
# cannot glue two unrelated cells into a token that looks like a match.
_BOX = str.maketrans({c: " " for c in "│╭╮╰╯─┌┐└┘┏┓┗┛┃┳┻╋╇╉┡┩├┤━"})


def _flowed(out):
    """Collapse hard wraps: 'flightdeck doctor\\n─' -> 'flightdeck doctor ─'.

    Recovers a WRAP. Cannot recover a FOLD (no space was printed at the break).
    """
    return " ".join(out.translate(_BOX).split())


def _says(out, phrase):
    """True if the render spells `phrase` once every space/newline is removed.

    Recovers a FOLD as well as a WRAP — use it only where a fold is what the render
    actually does, and keep a raw status marker beside it as proof of the row.
    """
    return "".join(phrase.split()) in "".join(out.translate(_BOX).split())
