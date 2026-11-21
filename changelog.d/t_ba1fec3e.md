kind: Added
task: t_ba1fec3e

- **`hscc doctor` now says what is parked, and says it as information.** The
  node-pause overlay had grown six repair-path skips (#565, #564, #568, #566 and
  the watchdog), an API projection (#569) and the `hscc cluster status` / `monitor`
  wording (t_480cee2d), while the one surface an operator runs to ask "why is this
  machine not serving" printed nothing about it. The new row is an INFO row
  (`ok=True`, `fatal=False`) carrying host, reason, `since` and `until`: a pause is
  the operator's own decision, so it must never read as WARN (○) or FAIL (✗), and a
  FATAL there would abort bootstrap Stage 1 over a parked worker. Nothing parked is
  silent — no host, no reason, no window, no warning (the row itself stays, because
  doctor's rule is that a check never renders as absence-of-output).
---
kind: Fixed
task: t_ba1fec3e

- **A corrupt `node_pauses.json` is now a WARN row instead of an invisible one.**
  `node_pauses.load()` normalises a truncated, non-JSON or entry-shaped-wrong
  overlay down to "no pauses" and reports the problem only through its own
  `_warn()` — the fail-open that `heal_breaker`, `autoscale` and `alerts`
  deliberately inherit, because a repair path must not stop healing over a bad file.
  For a report it is the dangerous half: the operator was told "no node pauses"
  about a node they had parked, while every repair loop treated that node as free.
  The row now goes `ok=False` (`fatal=False`) naming the overlay path, and quotes
  what the reader could not use — the warnings are captured from `node_pauses`
  around one `load()` (and restored in a `finally`) rather than re-parsed here, so
  "what counts as malformed" stays defined in exactly one module.
---
kind: Verified
task: t_ba1fec3e

- `hscc-bootstrap/tests/test_doctor_node_pauses.py` — 20 tests, green under the
  runner's interpreter (`~/.hermes/hermes-agent/venv/bin/python`) at `f0c09370`, each
  reading a real overlay file under a temp root through the overlay module's own
  `directory=` seam, TEST-NET-1 hosts only. The three postures are pinned
  field-by-field (`ok`, `fatal`, detail contents), plus: `since` keeps its `+00:00`
  offset while fractional seconds are trimmed, a partially readable overlay stays
  loud AND lists what it could read, and the default-root read is refused inside a
  pytest process — pinned by patching the reader to raise, so a leak fails rather
  than reading the operator's overlay.
- The root the row may use is caller-threaded only: the registry entry declares
  `row_kwargs=()`, because `node_pauses` has no `HSCC_DIR` override (its default is
  HOME's `~/.hscc`, resolved at import — exactly what `hscc node pause` writes), and a
  root taken from the environment is the t_adb66608 / t_be690204 split again: a
  directory no pause writer can reach, read back as "nothing is parked".
