"""Normalisers for Rich-rendered CLI output asserted by this leg's tests.

The daemon-leg copy of ``hscc-project/tests/rich_layout.py`` — that file is the
canonical statement of WHY there are two normalisers and what must stay RAW.
It is copied, not imported, for the same reason ``real_hermes.py`` is per-leg:
the legs deploy as standalone plugin dirs, so a flightdeck module is not
importable from this leg (and, per the repo ``pytest.ini``, the two legs are
never collected in one pytest process — so the shared module name is safe).
Import it the way this leg imports ``real_hermes``: ``from
hscc_daemon.tests.rich_layout import _says``.

Short version, so a reader of this leg does not have to leave it:

A Rich view lays a row out at *render* time from the data inside it, so a phrase
far enough into a row breaks at a position the test does not control. On these
views the width is fixed (80 for the daemon's plain Console on a non-tty) and the
only variable is how long the row's data is — here, the ``tmp_path``.

* **WRAP** — Rich breaks at a word boundary and the break consumes the space.
  ``_flowed`` collapses hard wraps and the box-drawing gutter.
* **FOLD** — the token is longer than the room left (a ``tmp_path`` always is) and
  Rich breaks it with **no** space. A word-boundary reflow cannot undo that: it
  puts a space where no space was ever printed. ``_says`` strips all whitespace,
  which recovers a fold and still matches everything ``_flowed`` matches.

Use the stricter of the two that recovers the render. Both are display-level only
— they say "the row does say this".

What must stay RAW: the row's status/leading markers (``UNEVALUABLE``, ``NOT
loaded yet``) — nothing can wrap or fold the first token of a row, and they are
the proof the row was rendered at all. An assert that only passes after whitespace
removal has to keep such an anchor beside it. And anything Rich **CLIPPED** (an
ellipsis replaced characters): no normaliser may recover that, and asserting a
folded form of it would assert words the render never printed.

Measured in t_89804ce6 (verbatim tee of the render, not pytest's elided ``E``
line): ``api install-service`` folds the unit-file path from tmp root 161, the
``autodown status`` interlock row folds the agent-source path at tmp roots
68..73 and 144..149.
"""

# Box-drawing and block characters: Rich's frame, and the gutter a wrapped
# continuation line is padded with. Space-substituted (not deleted) so a fold
# across a boundary cannot glue two unrelated cells into one matching token.
_BOX = str.maketrans({c: " " for c in "│╭╮╰╯─┌┐└┘┏┓┗┛┃┳┻╋╇╉┡┩├┤━"})


def _flowed(out):
    """Collapse hard wraps: 'autodown status\\n─' -> 'autodown status ─'.

    Recovers a WRAP. Cannot recover a FOLD (no space was printed at the break).
    """
    return " ".join(out.translate(_BOX).split())


def _says(out, phrase):
    """True if the render spells `phrase` once every space/newline is removed.

    Recovers a FOLD as well as a WRAP — use it only where a fold is what the
    render actually does, and keep a raw marker beside it as proof of the row.
    """
    return "".join(phrase.split()) in "".join(out.translate(_BOX).split())
