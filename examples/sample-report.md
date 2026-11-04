# Token audit 2026-01-07 (last 7 days)

Runs: 10 (2 main threads), requests: 102, total 740 978 WT. WT = weighted tokens (cache read 0.1, cache write 1.25, output 5). Sizes marked ~ are estimates, characters/3.5.

## Spend per agent

| Agent | Runs | Req | WT | Share | Startup ctx median | Output | Model | Effort |
|---|---|---|---|---|---|---|---|---|
| (main thread) | 2 | 46 | 352 780 | 47.6 % | 15 528 | 22 865 | opus-5-5 | medium |
| browser-checker | 2 | 36 | 264 900 | 35.7 % | 7 546 | 20 016 | sonnet-5-5 | medium |
| code-reviewer | 5 | 13 | 69 485 | 9.4 % | 5 125 | 5 874 | opus-5-5 | high |
| docs-writer | 1 | 7 | 53 814 | 7.3 % | 10 520 | 3 942 | sonnet-5-5 | medium |

## Top eaters (concrete sources of spend)

1. screenshots `browser-checker`: 32 images, ~43 680 tok -> ~97 734 WT (13.2 %)
2. reading `docs/api-spec.md`: 4x, ~24 000 tok added ((main thread), docs-writer) -> ~71 400 WT (9.6 %)
3. skill `style-guide` loaded 1x ((main thread)) -> ~25 848 WT (3.5 %)
4. tool definitions `browser-checker` ~4 100 tok (9 tools) -> ~24 190 WT (3.3 %)
5. system prompt `browser-checker` ~3 429 tok x 2 runs / 36 req -> ~20 231 WT (2.7 %)
6. system prompt `code-reviewer` ~2 571 tok x 5 runs / 13 req -> ~18 126 WT (2.4 %)
7. tool definitions `code-reviewer` ~2 536 tok (5 tools) -> ~17 879 WT (2.4 %)
8. system prompt `docs-writer` ~8 571 tok x 1 run / 7 req -> ~15 856 WT (2.1 %)
9. reading `src/cart_0.py`: 4x, ~3 429 tok added ((main thread), code-reviewer) -> ~9 256 WT (1.2 %)
10. reading `src/cart_1.py`: 4x, ~3 429 tok added ((main thread), code-reviewer) -> ~8 741 WT (1.2 %)

## Proposals with estimated savings (WT in the measured period)

1. **short subagent runs** - `code-reviewer`: 5 of 5 runs had <= 4 requests, median startup context ~5 125 tok; do simple lookups in the main thread or batch them. Saving ~48 639 WT. Type: structural.
2. **screenshots** - `browser-checker`: 32 images, ~43 680 tok (estimate width*height/750); read the page as text or cap screenshots per page. Saving ~39 094 WT. Type: prompt rule.
3. **repeated read** - `docs/api-spec.md`: read again 2x within the same run, 4 reads in 2 runs ((main thread) 3, docs-writer 1). Saving ~34 800 WT. Type: prompt rule: read once, then grep or offset.
4. **unused tool grant** - `browser-checker`: 6 tools with ~2 959 tok of definitions never called in 2 runs (in the `tools:` line: Grep, Read, WebFetch, mcp__browser__console_messages, mcp__browser__form_input, mcp__browser__upload_file). Saving ~17 458 WT. Type: mechanical, if the prompt does not describe a rare use.
5. **unused tool grant** - `code-reviewer`: 4 tools with ~1 989 tok of definitions never called in 5 runs (in the `tools:` line: Bash, Glob, Grep, WebSearch). Saving ~14 022 WT. Type: mechanical, if the prompt does not describe a rare use.
6. **preloaded skill** - `docs-writer`: `skills:` style-guide = ~6 082 tok in every request; saving if halved or loaded only when needed. Saving ~5 626 WT. Type: structural.
7. **repeated read** - `src/cart_0.py`: read again 2x within the same run, 4 reads in 2 runs ((main thread) 3, code-reviewer 1). Saving ~4 628 WT. Type: prompt rule: read once, then grep or offset.
8. **repeated read** - `src/cart_1.py`: read again 2x within the same run, 4 reads in 2 runs ((main thread) 3, code-reviewer 1). Saving ~4 285 WT. Type: prompt rule: read once, then grep or offset.
9. **repeated read** - `src/pricing_0.py`: read again 2x within the same run, 3 reads in 1 run ((main thread) 3). Saving ~2 356 WT. Type: prompt rule: read once, then grep or offset.
10. **repeated read** - `src/pricing_1.py`: read again 2x within the same run, 3 reads in 1 run ((main thread) 3). Saving ~2 071 WT. Type: prompt rule: read once, then grep or offset.

## Model and effort to review one by one

Agents on Opus or with high effort whose runs are short with little output. A pointer for a human decision, not a rule: an agent that has to get facts right may need the bigger model anyway.

- `code-reviewer` (opus/high): 2.6 req/run, 1 175 output tok/run

## Definition size (static, all agents)

| Agent | definition ~tok | preloaded skills ~tok | tools granted | model/effort | runs in period |
|---|---|---|---|---|---|
| docs-writer | 93 | 6 082 | 4 | sonnet/medium | 1 |
| browser-checker | 146 | 0 | 9 | sonnet/medium | 2 |
| code-reviewer | 114 | 0 | 5 | opus/high | 5 |

## Skills (invocations)

- `release-notes`: 1x, ~276 WT carried in context
- `style-guide`: 1x, ~25 848 WT carried in context

## Limits of the measurement

- WT does not compare models with each other.
- Sizes of prompts, tools, reads and skills are estimates (characters/3.5); API usage numbers are exact.
- Carry assumes content stays in context until the run ends; compaction and clearing of old tool results shorten that, so carry is an upper estimate. Total WT per agent is exact.
- The main thread also carries the work of skills and orchestration; their share is only estimated via SKILL.md size.
