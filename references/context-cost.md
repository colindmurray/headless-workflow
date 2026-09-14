# Context cost and harness fit

**Prefer a `pi-*` route for a wide fan-out whose provider supports pi.** The
same 25-agent graph — 20 parallel research steps, 4 analyses, 1 report — run
twice on `gpt-5.6-luna` at low effort, once through `codex` and once through
`pi` at `context: lean`:

| | codex | pi (lean) |
| --- | --- | --- |
| Prompt tokens, mean per worker | 34,610 | 1,528 |
| Prompt tokens, total | 865,250 | 39,728 |
| Total tokens | 871,893 | 42,313 |
| Median step | 42.0s | 11.5s |
| Wall clock | 132s | 70s |
| Steps at exit 0 | 25/25 | 26/26 |

Both produced 20 digests, 4 analyses, and a report of the same length reaching
the same recommendation — a 20x token saving and a 1.9x speedup at equal output.

The saving comes from `context: lean`, not from pi itself. Codex loads the whole
skill tree into every worker and cannot be told not to; it even warns that it
truncated skill descriptions to fit. Pi at `context: standard` costs 16,450
tokens against codex's 19,065, only 14% better. So the recommendation is
specifically **pi plus lean context**, and raising a pi route to `standard`
gives most of the advantage back.

Stay on the non-pi routes when a step needs MCP tools, sub-delegation, a richer
built-in toolset — pi has only `read`, `bash`, `edit`, `write`, `grep`, `find`,
`ls` — or a skill actually loaded into the worker.

