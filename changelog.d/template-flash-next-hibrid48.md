kind: Added

- **`4node-flash-next-hibrid48` template.** Same uniform 4-node Flash-Next
  shape as `4node-flash-next-all` (uniform pool, distinct ports, coding family,
  least-in-flight, xhigh/medium/low tiers), on myllmbox's vLLM build serving
  `myllmbox/Qwen3.8-Flash-Next-hibrid48`. Measured on one GB10 per candidate
  (llama-benchy 0.4.0, code corpus, 3 runs): ~53 t/s on one stream at 32k-258k
  depth and ~99-105 t/s per node at 4-10 streams (32k), against ~30 t/s on one
  stream for the eugr NVFP4 recipe. `max_num_seqs: 5` is sized to the recipe's
  ~728k-token KV pool (five 128k requests fit, six fall off a measured cliff).
---
kind: Changed

- **`4node-flash-next-all` caps 12/3 -> 6/2**, the operator's live kanban caps.
  The shipped value would otherwise rewrite the live config on the next
  `hscc template apply`. `TestShippedTemplateCapsMatchSOT` now pins 6/2 and
  checks both flash-next templates.
