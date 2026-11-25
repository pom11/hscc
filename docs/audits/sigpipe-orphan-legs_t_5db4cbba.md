# An untrapped `SIGPIPE` kills a leg pack and orphans both legs in flight (t_5db4cbba)

Card: `t_5db4cbba`. Split off `t_b94a2b39` (`docs/audits/leg-tmp-leak_t_b94a2b39.md`
§6 LEFT item 1). Base: `origin/devops/t_b94a2b39@ff0eb34f` (stacked on PR #825 — the
`_on_fatal_signal` this card extends does not exist on `main` yet).

**Status: IN PROGRESS.** Findings are appended as measured.

---

## 0. The question this card was handed

`t_b94a2b39` routed `HUP`/`QUIT` through `_on_fatal_signal` and left `SIGPIPE` out,
with the reason written into the handler's own comment:

> SIGPIPE is deliberately NOT routed here: it measured never to leak the dir, and
> trapping it in a 967-line script that pipes through `head`/`tr` changes error
> paths this card has no evidence for.

The measured cost of leaving it out (`leg-tmp-leak_t_b94a2b39.md` §5): **2.0 mean
legs orphaned** — the same cost `on_sigterm`'s comment says must never be paid
(legs burning CPU on the host that runs the fleet control plane).

So the card had to produce evidence about the paths it declined to guess at:

1. enumerate the `| head` / `| tr` / `| wc -l` sites and say what each does today
   when the consumer closes early (measure, not reason);
2. then either route `PIPE` through `_on_fatal_signal PIPE 141` or rule it must stay
   untrapped and fix the orphans another way.

## 1. Findings so far

_(appended below as measured)_
