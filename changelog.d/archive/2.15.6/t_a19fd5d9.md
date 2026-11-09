kind: Fixed
task: t_a19fd5d9

- **`hscc verify` false-failed a deliberate `cluster down` even after the
  stay-down gates landed.** t_ba1424b7 stopped `check_workers` and `check_proxy`
  from undoing the down, and t_50c6edc4 stopped the notifier paging — but the
  two checks that *probe* rather than heal kept writing a bare `ok: False` with
  no explanation while the fleet sat down on purpose. `hscc verify` excuses a
  stream only when the stream data carries its own `intentional` marker **and**
  `autodown.classify` confirms the window, and a manual down latches no watchdog
  block, so neither half held: an operator who typed `cluster down` still got a
  RED fleet for obeying their own instruction. `check_dgx` and `check_gateway`
  now read the same durable record the t_ba1424b7 gates read, through the same
  `health._operator_down()` seam, and — **only on a tick that is already
  failing** — stamp `intentional: "operator_down"` plus a message naming the
  record's reason
  (`fleet deliberately down — dgx unhealthy is expected while the fleet is
  down, not a fault (reason: hscc cluster down)`).
  **Streams stay truthful (D1):** `ok` is exactly what the probes determined.
  A unit that is not serving is not serving; a stamp explains a failure, it
  never launders one. Nothing about what the probes probe, what `ok` means, the
  returned verdicts or any notification changed, and a *healthy* stream during a
  stay-down is deliberately **not** stamped.
  **Precedence:** the new branch sits *after* the existing autodown branch, so
  when an autodown window and a leftover stay-down record both apply the
  autodown stamp wins — one marker, its existing wording, every existing
  autodown/verify test byte-identical. t_b543e530's invariant is untouched: a
  degraded (unverified) fleet-visibility read is still never excused, record or
  no record, because an unknown fleet state must not be claimed down by design.
  **Fail-safe:** a predicate that raises stamps nothing — the check behaves
  exactly as it did before the record existed ("no verifiable evidence" must
  never mean "go quiet"). This is the writer half; the reader half (verify
  excusing an `operator_down` stream) is a separate card, so `hscc verify` is
  unchanged by this fragment until that one lands.
