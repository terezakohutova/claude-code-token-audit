# Claude Code token audit

Finds out which of your Claude Code subagents waste tokens, and why. It reads the local session logs and checks every agent for tool definitions it carries in every request but never calls, screenshots that stay in context, files read whole again and again, preloaded skills and short runs that cost more to start than they do. Then it writes ranked proposals with a measured savings estimate.

One Python file, standard library only, no network. A subagent definition with a persona (Tomáš Baťa) runs it for you and edits a definition only when you say yes.

## Run it

```
python token_audit.py --repo path/to/your/project
```

That audits the session files changed in the last 7 days in `~/.claude/projects` (or `$CLAUDE_CONFIG_DIR/projects`) against the agents in `path/to/your/project/.claude/agents`. Useful flags:

| Flag | What it does |
|---|---|
| `--days 14` | another window (by file modification time, so a resumed older session counts whole) |
| `--top 15` | longer lists of eaters and proposals |
| `--agent code-reviewer` | one subagent type only |
| `--out report.md`, `--json agg.json` | also write the report or the raw aggregates to a file |
| `--list-sessions`, `--session "checkout"` | one row per named session, main thread plus its subagents |
| `--projects DIR` | another log folder |

Try it without your own logs first:

```
python examples/make_synthetic_logs.py demo-logs # git-ignored
python token_audit.py --projects demo-logs/projects --repo examples/demo-project
```

## What came out on the demo

The demo is a made-up web shop with three subagents: `code-reviewer` (Opus, high effort), `browser-checker` (takes screenshots) and `docs-writer` (preloads a long style guide). Its logs are written by [examples/make_synthetic_logs.py](examples/make_synthetic_logs.py), and [examples/sample-report.md](examples/sample-report.md) is the unedited output of the command above (made with a fixed report date). On every CI run a test regenerates the logs and the report and fails if the report differs. The top proposals:

1. **short subagent runs** - `code-reviewer`: 5 of 5 runs had at most 4 requests, median startup context ~5 125 tok. Saving ~48 639 WT.
2. **screenshots** - `browser-checker`: 32 images, ~43 680 tok. Saving ~39 094 WT.
3. **repeated read** - `docs/api-spec.md` read again twice within the same run. Saving ~34 800 WT.
4. **unused tool grant** - `browser-checker`: 6 of its 9 tools never called (`Grep, Read, WebFetch, mcp__browser__console_messages, mcp__browser__form_input, mcp__browser__upload_file`). Saving ~17 458 WT.

It also flags `code-reviewer` for a human look: Opus at high effort for 2.6 requests and about 1 200 output tokens per run.

## What it measures

| Metric | Source in the logs | Precision |
|---|---|---|
| Tokens per agent (input, cache write, cache read, output) | `message.usage`, deduplicated by message id + request id | exact |
| Startup context of a run | usage of the first request | exact |
| System prompt and tool definition size | `api-request-shape` record (`system`, `tools`) | estimate, characters/3.5 |
| Unused tool grants | tools in `api-request-shape` vs. actual `tool_use` calls | exact counts, estimated tokens |
| Screenshots | images in tool results, width*height/750 | estimate |
| Big and repeated file reads | `Read` results, repeats within one run | estimate |
| Short subagent runs | runs with at most 4 requests | exact |
| Skills | `Skill` calls and slash commands, plus `SKILL.md` size | estimate |
| Model and effort | `message.model`, `effort` | exact |

**WT (weighted tokens)** = input + 1.25 * cache write 5 min + 2 * cache write 1 h + 0.1 * cache read + 5 * output. A relative cost in "fresh input token" units of the same model, from the ratios in Anthropic's price list. It does not compare Opus with Sonnet.

**Carry.** Content that enters the context at request *i* is paid once as a cache write and then as a cache read in every later request of that run. A 20k-token file read at the start of a 200-request run costs about 25k + 200 * 20k * 0.1 = 425k WT. Compaction shortens this, so carry is an upper estimate; the totals per agent are exact.

`api-request-shape` records appear in logs from about Claude Code 2.1.300 on. Older logs still get the token numbers, and the report says what it could not measure.

## Privacy

Session logs hold your conversations. The report carries only numbers, agent, skill, tool and model names, session names and the paths of files that were read (relative to the project, or with your home folder shown as `~`). No prompt, reply or tool output. A test fills the synthetic logs with a marker string in every prompt, reply, tool result and system prompt and fails if the marker shows up in the report, the JSON or the session table. Keep reports out of git anyway, since file paths and session names can say more than you want.

## The agent

[.claude/agents/token-auditor.md](.claude/agents/token-auditor.md) runs the script, checks each finding against the actual definition (a tool that is never called may still be described for a rare case), ranks proposals and edits a definition only on your explicit go. Structural changes (merging agents, a different model) it only proposes. It speaks as Tomáš Baťa, the Czech shoemaker who ran his company on numbers measured every day, in chat only.

To use it, copy `token_audit.py` and `.claude/agents/token-auditor.md` into your project, keeping the same relative layout, and add `token-audit-reports/` to `.gitignore`.

## Prior art

Several good tools already count Claude Code tokens. This one does not replace them:

- [ccusage](https://github.com/ryoppippi/ccusage) (MIT): usage and cost by day, month, session, 5-hour block, model and project. The message id + request id deduplication here is the same approach; no code is taken from it.
- Claude Code's own `/usage` (recent versions): share of usage by skill, subagent, plugin and MCP server.
- [token-reporter-plugin](https://github.com/emasoft/token-reporter-plugin) (MIT): hook-based report per subagent, skill and tool for the current session.
- [token-optimizer](https://github.com/alexgreensh/token-optimizer) (PolyForm Noncommercial): per-component audit, re-read detection and coaching with savings estimates.

What this one adds: unused tool grants measured from the real request shape, screenshot cost per agent, the carry model for anything that stays in context, a static size audit of your agent definitions next to their measured runs, and all of it as one offline MIT script you can read in ten minutes.

## Tests

```
python -m unittest discover -s tests -v
```

CI runs them on Linux and Windows with Python 3.10 and 3.13.

## License

MIT, see [LICENSE](LICENSE).
