# Closing the round-4 publish channels: child authorship + `__pycache__` (t_38fd345f)

Status: in progress (this file is the running record; sections are appended as
measured, not written up afterwards).

Follow-up to `ci-backstop-address-guard-t_9a4b7687.md`. Reviewer round 4
(run 966) reproduced two publish channels *inside* the round-3 child boundary,
on py3.11.16 + py3.13.7, through the shipping step script and the redactor CLI
at gated tip `8d73cf1f` (now an ancestor of `main`, released as 2.5.8):

* **D0** — the child protocol validated the FORM of a verdict, never its
  AUTHORSHIP: a guard module body could write a well-formed `OK` line itself
  and `os._exit(0)`, so the parent saw exactly one honest-looking verdict and
  honoured it. Advisory job GREEN, value in the public log.
* **D1** — the child's `spec_from_file_location` import honoured a committed
  `scripts/__pycache__/*.pyc`; an unchecked-hash pyc (PEP 552, official
  `compileall --invalidation-mode unchecked-hash` artifact) is trusted WITHOUT
  reading the source, so the child redacted with the pyc's pattern while the
  CLI's detection stayed honest. Advisory GREEN, value printed.

## Baseline at the pre-fix tip (measured)

(filled in below by execution — probe_r4_forged_exit.py, probe_r4_pyc_step.py,
both interpreters, worktree copy of the shipped tip)

## The fix (design)

## Results at the new tip

## The residual, named so round 5 cannot re-open it
