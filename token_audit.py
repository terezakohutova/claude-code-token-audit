#!/usr/bin/env python3
"""Token audit for Claude Code: measured token spend per agent and skill from the
local session logs (JSONL), plus concrete savings proposals. Standard library only,
reads locally, writes locally, never sends anything anywhere.

Usage:
  python token_audit.py                          # last 7 days, report to stdout
  python token_audit.py --days 14 --top 15 --out report.md
  python token_audit.py --agent code-reviewer    # one subagent type only
  python token_audit.py --json aggregates.json   # machine-readable aggregates too
  python token_audit.py --list-sessions          # one row per named session
  python token_audit.py --session "checkout"     # sessions whose name contains the text

Where it reads:
  logs         --projects, else $CLAUDE_CONFIG_DIR/projects, else ~/.claude/projects
  definitions  --repo (default: current directory)/.claude/agents and .claude/skills

The report contains only aggregated numbers, agent, skill, tool and model names,
session names and the paths of files that were read. No prompt text, no tool
output, no message content.

Units
- Raw tokens come from the API usage records in the logs (exact).
- WT (weighted tokens) = input + 1.25*cache_write_5m + 2*cache_write_1h
  + 0.1*cache_read + 5*output. Relative cost in "fresh input token" units of
  the same model, following the ratios in Anthropic's published price list for
  cache and output. WT does not compare models with each other.
- Sizes of prompts, tool definitions, file reads and skills are ESTIMATES:
  characters / 3.5. Marked "~" in the report.
"""
import argparse
import collections
import datetime as dt
import json
import os
import re
import statistics
import sys
import unicodedata

__version__ = "1.0.0"

CHARS_PER_TOKEN = 3.5
W_IN, W_CW5, W_CW1H, W_CR, W_OUT = 1.0, 1.25, 2.0, 0.1, 5.0
BIG_READ_TOKENS = 3000          # a single read above this counts as a "big read"
SHORT_RUN_REQUESTS = 4          # subagent run with <= this many API requests = "short run"
IMG_DEFAULT_TOKENS = 1000       # image without a known size (estimate)
MAIN = "(main thread)"
UNKNOWN_SUBAGENT = "(unknown subagent)"
NEVER_FLAG = ("DeferredToolPlaceholder", "SubagentHandback", "advisor")


def default_projects():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
    return os.path.join(base, "projects")


def est_tokens(chars):
    return int(round(chars / CHARS_PER_TOKEN))


def wt(u):
    cc = u.get("cache_creation") or {}
    cw1h = cc.get("ephemeral_1h_input_tokens", 0) or 0
    cw_total = u.get("cache_creation_input_tokens", 0) or 0
    cw5 = max(cw_total - cw1h, 0)
    return (W_IN * (u.get("input_tokens", 0) or 0) + W_CW5 * cw5 + W_CW1H * cw1h
            + W_CR * (u.get("cache_read_input_tokens", 0) or 0)
            + W_OUT * (u.get("output_tokens", 0) or 0))


def content_chars(c):
    """Length of a tool_result content (str or list of blocks), image count and estimated
    image tokens. Image tokens ~ width*height/750 (Anthropic's vision rule of thumb); the
    size is parsed from a '(WxH' note next to the image (the last note covers any
    further images), else IMG_DEFAULT_TOKENS."""
    if isinstance(c, str):
        return len(c), 0, 0
    n, imgs, img_tok = 0, 0, 0
    dims = []
    if isinstance(c, list):
        for b in c:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                txt = b.get("text", "")
                n += len(txt)
                dims += [(int(w), int(h)) for w, h in re.findall(r"\((\d{2,4})x(\d{2,4})", txt[:300])]
            elif b.get("type") == "image":
                imgs += 1
        for i in range(imgs):
            w, h = dims[min(i, len(dims) - 1)] if dims else (0, 0)
            img_tok += (w * h // 750) if dims else IMG_DEFAULT_TOKENS
    return n, imgs, img_tok


# ---------------------------------------------------------------- definitions
def parse_frontmatter(path):
    try:
        with open(path, encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return {}, 0
    m = re.match(r"---\n(.*?)\n---\n", txt.replace("\r\n", "\n"), re.S)
    fm = {}
    if m:
        key = None
        for ln in m.group(1).split("\n"):
            km = re.match(r"^([A-Za-z_-]+):\s*(.*)$", ln)
            if km:
                key = km.group(1)
                fm[key] = km.group(2).strip()
            elif key and ln.strip().startswith("- "):
                fm[key] = (fm[key] + "," if fm[key] else "") + ln.strip()[2:].strip()
    return fm, len(txt)


def load_definitions(repo):
    agents, skills = {}, {}
    adir = os.path.join(repo, ".claude", "agents")
    for fn in sorted(os.listdir(adir)) if os.path.isdir(adir) else []:
        if fn.endswith(".md"):
            fm, n = parse_frontmatter(os.path.join(adir, fn))
            agents[fm.get("name", fn[:-3])] = {
                "file": os.path.join(".claude", "agents", fn),
                "chars": n,
                "tools": [t.strip() for t in fm.get("tools", "").split(",") if t.strip()],
                "model": fm.get("model", "?"),
                "effort": fm.get("effort", "?"),
                "skills": [s.strip() for s in fm.get("skills", "").split(",") if s.strip()],
            }
    sdir = os.path.join(repo, ".claude", "skills")
    for d in sorted(os.listdir(sdir)) if os.path.isdir(sdir) else []:
        p = os.path.join(sdir, d, "SKILL.md")
        if os.path.isfile(p):
            fm, n = parse_frontmatter(p)
            skills[fm.get("name", d)] = {"file": os.path.join(".claude", "skills", d, "SKILL.md"), "chars": n}
    return agents, skills


# ---------------------------------------------------------------- transcripts
def new_run(agent, session, path, builtin=False):
    return {"agent": agent, "session": session, "path": path, "builtin": builtin,
            "requests": 0, "in": 0, "cw": 0, "cr": 0, "out": 0, "think": 0, "wt": 0.0,
            "startup": None, "models": collections.Counter(), "efforts": collections.Counter(),
            "sys_chars": 0, "tools_chars": {}, "tools_used": collections.Counter(),
            "results": [],   # (request_index, tool, key, chars, images, image_tokens)
            "skills": [],    # (request_index, skill_name)
            "start": None, "cwd": None,
            "title": None,          # session name (custom-title), main thread only
            "agent_setting": None}  # agent the main thread runs as (claude --agent X)


def scan_transcript(path, run):
    """One pass over a JSONL transcript. Only counts, names and paths are kept."""
    seen_req = set()
    tool_by_id = {}
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return run
    with fh:
        for line in fh:
            head = line[:120]
            if '"api-request-blob"' in head or '"type":"api-request"' in head:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            if not isinstance(o, dict):
                continue
            t = o.get("type")
            if run["start"] is None and o.get("timestamp"):
                run["start"] = o["timestamp"]
            if run["cwd"] is None and isinstance(o.get("cwd"), str):
                run["cwd"] = o["cwd"]
            if t == "custom-title":
                run["title"] = str(o.get("customTitle") or "") or run["title"]
                continue
            if t == "agent-setting":
                run["agent_setting"] = str(o.get("agentSetting") or "") or run["agent_setting"]
                continue
            if t == "api-request-shape" and not run["tools_chars"]:
                shape = o.get("shape") or {}
                if isinstance(shape, dict):
                    run["sys_chars"] = sum(len(b.get("text", "")) for b in shape.get("system", []) if isinstance(b, dict))
                    for tdef in shape.get("tools", []) or []:
                        if isinstance(tdef, dict):
                            run["tools_chars"][tdef.get("name", "?")] = len(json.dumps(tdef, ensure_ascii=False))
            elif t == "assistant":
                m = o.get("message") or {}
                key = (m.get("id"), o.get("requestId"))
                for b in m.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        name = b.get("name", "?")
                        run["tools_used"][name] += 1
                        inp = b.get("input") or {}
                        k = inp.get("file_path") or ""
                        tool_by_id[b.get("id")] = (name, str(k))
                        if name == "Skill":
                            run["skills"].append((run["requests"], str(inp.get("skill", "?"))))
                if key in seen_req:
                    continue
                seen_req.add(key)
                u = m.get("usage") or {}
                if not u:
                    continue
                run["requests"] += 1
                run["in"] += u.get("input_tokens", 0) or 0
                run["cw"] += u.get("cache_creation_input_tokens", 0) or 0
                run["cr"] += u.get("cache_read_input_tokens", 0) or 0
                run["out"] += u.get("output_tokens", 0) or 0
                run["think"] += ((u.get("output_tokens_details") or {}).get("thinking_tokens", 0) or 0)
                run["wt"] += wt(u)
                if run["startup"] is None:
                    run["startup"] = ((u.get("input_tokens", 0) or 0) + (u.get("cache_creation_input_tokens", 0) or 0)
                                      + (u.get("cache_read_input_tokens", 0) or 0))
                run["models"][m.get("model", "?")] += 1
                if o.get("effort"):
                    run["efforts"][str(o.get("effort"))] += 1
            elif t == "user":
                m = o.get("message") or {}
                c = m.get("content")
                if isinstance(c, str):
                    for cm in re.findall(r"<command-name>/?([\w:-]+)</command-name>", c):
                        run["skills"].append((run["requests"], cm))
                    continue
                for b in c or []:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        name, k = tool_by_id.get(b.get("tool_use_id"), ("?", ""))
                        n, imgs, img_tok = content_chars(b.get("content"))
                        run["results"].append((run["requests"], name, k, n, imgs, img_tok))
    return run


def collect(projects, since, only_agent=None):
    runs = []
    if not os.path.isdir(projects):
        return runs
    for proj in sorted(os.listdir(projects)):
        pdir = os.path.join(projects, proj)
        if not os.path.isdir(pdir):
            continue
        for root, _dirs, files in os.walk(pdir):
            for fn in sorted(files):
                if not fn.endswith(".jsonl"):
                    continue
                fp = os.path.join(root, fn)
                try:
                    if dt.datetime.fromtimestamp(os.path.getmtime(fp)) < since:
                        continue
                except OSError:
                    continue
                if os.path.basename(root) == "subagents":
                    agent, builtin = UNKNOWN_SUBAGENT, False
                    try:
                        with open(fp[:-6] + ".meta.json", encoding="utf-8") as f:
                            meta = json.load(f)
                        agent = meta.get("agentType") or agent
                        builtin = bool(meta.get("isBuiltIn"))
                    except (OSError, ValueError, AttributeError):
                        pass
                    session = os.path.basename(os.path.dirname(root))
                else:
                    agent, builtin, session = MAIN, False, fn[:-6]
                if only_agent and agent != only_agent:
                    continue
                run = scan_transcript(fp, new_run(agent, session, fp, builtin))
                if run["requests"]:
                    runs.append(run)
    return runs


# ---------------------------------------------------------------- analysis
def carry_wt(chars, req_idx, total_req):
    """WT for content entering context at request req_idx: written once, re-read until the run ends."""
    return est_tokens(chars) * (W_CW5 + W_CR * max(total_req - req_idx - 1, 0))


def short_path(key, cwd):
    p = key.replace("\\", "/")
    if cwd:
        base = cwd.replace("\\", "/").rstrip("/") + "/"
        if p.lower().startswith(base.lower()):
            return p[len(base):]
    return p


def analyse(runs, skills_def):
    by_agent = collections.defaultdict(lambda: {"runs": 0, "requests": 0, "in": 0, "cw": 0, "cr": 0, "out": 0,
                                                 "think": 0, "wt": 0.0, "startups": [], "sys_chars": [],
                                                 "tools_chars": {}, "tools_used": collections.Counter(),
                                                 "models": collections.Counter(), "efforts": collections.Counter(),
                                                 "short_runs": 0, "short_wt": 0.0, "img": 0, "img_tok": 0, "img_wt": 0.0,
                                                 "builtin": False})
    reads = collections.defaultdict(lambda: {"count": 0, "chars": 0, "wt": 0.0, "runs": set(),
                                             "agents": collections.Counter(), "dup": 0, "dup_wt": 0.0})
    skill_use = collections.defaultdict(lambda: {"count": 0, "wt": 0.0, "agents": collections.Counter()})
    tool_results = collections.defaultdict(lambda: {"count": 0, "chars": 0, "wt": 0.0})

    for r in runs:
        a = by_agent[r["agent"]]
        a["builtin"] = a["builtin"] or r["builtin"]
        a["runs"] += 1
        for k in ("requests", "in", "cw", "cr", "out", "think"):
            a[k] += r[k]
        a["wt"] += r["wt"]
        if r["startup"]:
            a["startups"].append(r["startup"])
        if r["sys_chars"]:
            a["sys_chars"].append(r["sys_chars"])
        for name, n in r["tools_chars"].items():
            a["tools_chars"][name] = max(a["tools_chars"].get(name, 0), n)
        a["tools_used"].update(r["tools_used"])
        a["models"].update(r["models"])
        a["efforts"].update(r["efforts"])
        if r["agent"] != MAIN and r["requests"] <= SHORT_RUN_REQUESTS:
            a["short_runs"] += 1
            a["short_wt"] += r["wt"]
        N = r["requests"]
        per_run_seen = collections.Counter()
        for idx, tool, key, chars, imgs, img_tok in r["results"]:
            cv = carry_wt(chars, idx, N)
            tr = tool_results[(r["agent"], tool)]
            tr["count"] += 1
            tr["chars"] += chars
            tr["wt"] += cv
            if imgs:
                a["img"] += imgs
                a["img_tok"] += img_tok
                a["img_wt"] += img_tok * (W_CW5 + W_CR * max(N - idx - 1, 0))
            if tool == "Read" and key:
                p = short_path(key, r["cwd"])
                rd = reads[p]
                rd["count"] += 1
                rd["chars"] += chars
                rd["wt"] += cv
                rd["runs"].add(r["path"])
                rd["agents"][r["agent"]] += 1
                per_run_seen[p] += 1
                if per_run_seen[p] > 1:
                    rd["dup"] += 1
                    rd["dup_wt"] += cv
        for idx, sk in r["skills"]:
            sd = skills_def.get(sk.split(":")[-1])
            if not sd and ":" not in sk:
                continue  # built-in command (/login, /compact, /model ...), not a skill
            chars = sd["chars"] if sd else 0
            s = skill_use[sk]
            s["count"] += 1
            s["agents"][r["agent"]] += 1
            s["wt"] += carry_wt(chars, idx, N) if chars else 0.0
    return by_agent, reads, skill_use, tool_results


def per_1k_prompt_wt(a):
    """WT saved in the period by removing 1000 tokens from this agent's always-loaded prompt."""
    return 1000 * (a["runs"] * W_CW5 + max(a["requests"] - a["runs"], 0) * W_CR)


def fmt(n):
    return f"{int(round(n)):,}".replace(",", " ")


def findings(by_agent, reads, agents_def, skills_def):
    """Concrete, measured proposals with a savings estimate (WT in the measured period)."""
    props = []
    for name, a in by_agent.items():
        d = agents_def.get(name)
        # 1) unused tool grants: definition carried in every request, never called
        if a["tools_chars"] and name != MAIN and not a["builtin"]:
            unused = [t for t in a["tools_chars"] if a["tools_used"].get(t, 0) == 0 and t not in NEVER_FLAG]
            if unused:
                tok = est_tokens(sum(a["tools_chars"][t] for t in unused))
                granted = set(d["tools"]) if d else set()
                listed = sorted(t for t in unused if t in granted) if granted else sorted(unused)
                props.append({"kind": "unused tool grant", "target": name, "wt": tok / 1000 * per_1k_prompt_wt(a),
                              "detail": f"{len(unused)} tools with ~{fmt(tok)} tok of definitions never called in {a['runs']} run{'s' if a['runs'] != 1 else ''}"
                                        f" (in the `tools:` line: {', '.join(listed[:8])}{'...' if len(listed) > 8 else ''})",
                              "type": "mechanical, if the prompt does not describe a rare use"})
        # 2) preloaded skills
        if d and d["skills"]:
            sk_chars = sum(skills_def.get(s, {}).get("chars", 0) for s in d["skills"])
            if sk_chars:
                tok = est_tokens(sk_chars)
                props.append({"kind": "preloaded skill", "target": name, "wt": tok / 1000 * per_1k_prompt_wt(a) * 0.5,
                              "detail": f"`skills:` {', '.join(d['skills'])} = ~{fmt(tok)} tok in every request;"
                                        f" saving if halved or loaded only when needed",
                              "type": "structural"})
        # 3) short subagent runs: startup overhead dominates
        if name != MAIN and a["short_runs"] >= 2 and a["startups"]:
            med = statistics.median(a["startups"])
            props.append({"kind": "short subagent runs", "target": name, "wt": a["short_wt"] * 0.7,
                          "detail": f"{a['short_runs']} of {a['runs']} runs had <= {SHORT_RUN_REQUESTS} requests,"
                                    f" median startup context ~{fmt(med)} tok; do simple lookups in the main thread or batch them",
                          "type": "structural"})
        # 4) screenshots
        if a["img"] >= 20:
            props.append({"kind": "screenshots", "target": name, "wt": a["img_wt"] * 0.4,
                          "detail": f"{a['img']} images, ~{fmt(a['img_tok'])} tok (estimate width*height/750);"
                                    f" read the page as text or cap screenshots per page",
                          "type": "prompt rule"})
    # 5) repeated and big reads
    for p, rd in reads.items():
        if rd["dup"] >= 2:
            props.append({"kind": "repeated read", "target": p, "wt": rd["dup_wt"],
                          "detail": f"read again {rd['dup']}x within the same run, {rd['count']} reads in {len(rd['runs'])} run{'s' if len(rd['runs']) != 1 else ''}"
                                    f" ({', '.join(f'{k} {v}' for k, v in rd['agents'].most_common(3))})",
                          "type": "prompt rule: read once, then grep or offset"})
        elif rd["count"] >= 5 and rd["chars"] / max(rd["count"], 1) > BIG_READ_TOKENS * CHARS_PER_TOKEN:
            props.append({"kind": "big file read whole", "target": p, "wt": rd["wt"] * 0.6,
                          "detail": f"{rd['count']} reads, average ~{fmt(est_tokens(rd['chars'] / rd['count']))} tok;"
                                    f" read a section (grep or offset) or shorten the file",
                          "type": "structural (file) or prompt rule"})
    props.sort(key=lambda x: (-x["wt"], x["kind"], x["target"]))
    return props


# ---------------------------------------------------------------- report
def report(runs, by_agent, reads, skill_use, tool_results, props, agents_def, skills_def, days, top, today):
    L = []
    total_wt = sum(a["wt"] for a in by_agent.values()) or 1.0
    L.append(f"# Token audit {today} (last {days} days)\n")
    L.append(f"Runs: {len(runs)} ({sum(1 for r in runs if r['agent'] == MAIN)} main threads), "
             f"requests: {fmt(sum(r['requests'] for r in runs))}, total {fmt(total_wt)} WT. "
             "WT = weighted tokens (cache read 0.1, cache write 1.25, output 5). Sizes marked ~ are estimates, characters/3.5.\n")
    L.append("## Spend per agent\n")
    L.append("| Agent | Runs | Req | WT | Share | Startup ctx median | Output | Model | Effort |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for name, a in sorted(by_agent.items(), key=lambda kv: (-kv[1]["wt"], kv[0])):
        med = int(statistics.median(a["startups"])) if a["startups"] else 0
        models = ", ".join(m.replace("claude-", "") for m, _ in a["models"].most_common(2))
        eff = ", ".join(e for e, _ in a["efforts"].most_common(2)) or (agents_def.get(name, {}).get("effort", "") if name in agents_def else "")
        L.append(f"| {name}{' (built-in)' if a['builtin'] else ''} | {a['runs']} | {fmt(a['requests'])} | {fmt(a['wt'])} | "
                 f"{a['wt'] / total_wt * 100:.1f} % | {fmt(med)} | {fmt(a['out'])} | {models} | {eff} |")

    L.append("\n## Top eaters (concrete sources of spend)\n")
    eaters = []
    for name, a in by_agent.items():
        if a["sys_chars"] and name != MAIN:
            tok = est_tokens(statistics.median(a["sys_chars"]))
            eaters.append((tok / 1000 * per_1k_prompt_wt(a), f"system prompt `{name}` ~{fmt(tok)} tok x {a['runs']} run{'s' if a['runs'] != 1 else ''} / {fmt(a['requests'])} req"))
        if a["tools_chars"] and name != MAIN:
            tok = est_tokens(sum(a["tools_chars"].values()))
            eaters.append((tok / 1000 * per_1k_prompt_wt(a), f"tool definitions `{name}` ~{fmt(tok)} tok ({len(a['tools_chars'])} tools)"))
        if a["img"]:
            eaters.append((a["img_wt"], f"screenshots `{name}`: {a['img']} images, ~{fmt(a['img_tok'])} tok"))
    for p, rd in reads.items():
        eaters.append((rd["wt"], f"reading `{p}`: {rd['count']}x, ~{fmt(est_tokens(rd['chars']))} tok added ({', '.join(k for k, _ in rd['agents'].most_common(2))})"))
    for (agent, tool), tr in tool_results.items():
        if tool != "Read":
            eaters.append((tr["wt"], f"`{tool}` results in `{agent}`: {tr['count']}x, ~{fmt(est_tokens(tr['chars']))} tok"))
    for sk, s in skill_use.items():
        if s["wt"]:
            eaters.append((s["wt"], f"skill `{sk}` loaded {s['count']}x ({', '.join(k for k, _ in s['agents'].most_common(2))})"))
    eaters.sort(key=lambda x: (-x[0], x[1]))
    for i, (v, txt) in enumerate(eaters[:top], 1):
        L.append(f"{i}. {txt} -> ~{fmt(v)} WT ({v / total_wt * 100:.1f} %)")

    L.append("\n## Proposals with estimated savings (WT in the measured period)\n")
    for i, p in enumerate(props[:top], 1):
        L.append(f"{i}. **{p['kind']}** - `{p['target']}`: {p['detail']}. Saving ~{fmt(p['wt'])} WT. Type: {p['type']}.")
    if not props:
        L.append("- nothing to propose")

    L.append("\n## Model and effort to review one by one\n")
    L.append("Agents on Opus or with high effort whose runs are short with little output. A pointer for a human"
             " decision, not a rule: an agent that has to get facts right may need the bigger model anyway.\n")
    flagged = 0
    for name, a in sorted(by_agent.items(), key=lambda kv: (-kv[1]["wt"], kv[0])):
        d = agents_def.get(name)
        if not d or not a["runs"]:
            continue
        heavy = "opus" in d["model"] or d["effort"] in ("high", "xhigh", "max")
        req_run, out_run = a["requests"] / a["runs"], a["out"] / a["runs"]
        if heavy and req_run <= 8 and out_run <= 8000:
            flagged += 1
            L.append(f"- `{name}` ({d['model']}/{d['effort']}): {req_run:.1f} req/run, {fmt(out_run)} output tok/run")
    if not flagged:
        L.append("- nothing to review")

    def sk_chars(d):
        return sum(skills_def.get(x, {}).get("chars", 0) for x in d["skills"])
    if agents_def:
        L.append("\n## Definition size (static, all agents)\n")
        L.append("| Agent | definition ~tok | preloaded skills ~tok | tools granted | model/effort | runs in period |")
        L.append("|---|---|---|---|---|---|")
        for name, d in sorted(agents_def.items(), key=lambda kv: (-(kv[1]["chars"] + sk_chars(kv[1])), kv[0])):
            L.append(f"| {name} | {fmt(est_tokens(d['chars']))} | {fmt(est_tokens(sk_chars(d)))} | {len(d['tools'])} | "
                     f"{d['model']}/{d['effort']} | {by_agent[name]['runs'] if name in by_agent else 0} |")
        gone = sorted(n for n in by_agent if n not in agents_def and n != MAIN and not by_agent[n]["builtin"])
        if gone:
            L.append(f"\nRuns of agents without a definition in .claude/agents/ (deleted, renamed or user-level?): {', '.join(gone)}")

    L.append("\n## Skills (invocations)\n")
    for sk, s in sorted(skill_use.items(), key=lambda kv: (-kv[1]["count"], kv[0]))[:top]:
        L.append(f"- `{sk}`: {s['count']}x, ~{fmt(s['wt'])} WT carried in context")
    if not skill_use:
        L.append("- none")

    L.append("\n## Limits of the measurement\n")
    L.append("- WT does not compare models with each other.")
    L.append("- Sizes of prompts, tools, reads and skills are estimates (characters/3.5); API usage numbers are exact.")
    L.append("- Carry assumes content stays in context until the run ends; compaction and clearing of old tool"
             " results shorten that, so carry is an upper estimate. Total WT per agent is exact.")
    L.append("- The main thread also carries the work of skills and orchestration; their share is only estimated via SKILL.md size.")
    if not any(a["tools_chars"] for a in by_agent.values()):
        L.append("- No `api-request-shape` records found (Claude Code before ~2.1.300): system prompt size,"
                 " tool definitions and unused tool grants cannot be measured.")
    return "\n".join(L) + "\n"


def _fold(s):
    return "".join(c for c in unicodedata.normalize("NFKD", s or "") if not unicodedata.combining(c)).casefold()


def run_images(r):
    return sum(x[4] for x in r["results"]), sum(x[5] for x in r["results"])


def session_report(runs, pattern, days):
    """One row per session: the main thread plus its subagents. Names and numbers only."""
    by_sess = collections.defaultdict(list)
    for r in runs:
        by_sess[r["session"]].append(r)
    rows = []
    for sid, rs in by_sess.items():
        mains = [r for r in rs if r["agent"] == MAIN]
        title = next((r["title"] for r in mains if r["title"]), None)
        if pattern is not None and (not title or _fold(pattern) not in _fold(title)):
            continue
        if not mains:
            continue
        m = mains[0]
        subs = [r for r in rs if r["agent"] != MAIN]
        rows.append({"session": sid, "title": title or "(unnamed)", "agent": m["agent_setting"] or "-",
                     "start": (m["start"] or "")[:16], "req": m["requests"], "wt": m["wt"], "img": run_images(m)[0],
                     "sub_runs": len(subs), "sub_wt": sum(r["wt"] for r in subs),
                     "sub_img": sum(run_images(r)[0] for r in subs)})
    rows.sort(key=lambda x: (x["start"], x["session"]))
    L = [f"# Token audit per session ({days} days{', filter: ' + pattern if pattern else ''})", "",
         "Main thread of each session plus its subagents. Session names and numbers only, no content.", "",
         "| Start (UTC) | Session | Agent | Req | WT main | Images main | Subagents | WT subagents | Images subagents | WT total | Images total |",
         "|---|---|---|---|---|---|---|---|---|---|---|"]
    for x in rows:
        L.append(f"| {x['start']} | {x['title'].replace('|', '/')} | {x['agent']} | {x['req']} | {x['wt']/1e6:.2f}M | {x['img']} | "
                 f"{x['sub_runs']} | {x['sub_wt']/1e6:.2f}M | {x['sub_img']} | {(x['wt']+x['sub_wt'])/1e6:.2f}M | {x['img']+x['sub_img']} |")
    L += ["", "Session = one JSONL file; a session resumed with --resume can show up as several rows with the same name."]
    return "\n".join(L) + "\n", rows


def write_out(md, out):
    if out:
        d = os.path.dirname(os.path.abspath(out))
        os.makedirs(d, exist_ok=True)
        with open(out, "w", encoding="utf-8", newline="\n") as f:
            f.write(md)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    print(md)
    if out:
        print(f"Report: {out}")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--projects", default=None, help="session log folder (default: $CLAUDE_CONFIG_DIR/projects or ~/.claude/projects)")
    ap.add_argument("--repo", default=".", help="project whose .claude/agents and .claude/skills are audited (default: current directory)")
    ap.add_argument("--agent", help="one subagent type only, e.g. code-reviewer")
    ap.add_argument("--out", help="also write the Markdown report to this file")
    ap.add_argument("--json", help="also write the aggregates as JSON to this file")
    ap.add_argument("--session", help="only sessions whose name contains this text (accents and case ignored)")
    ap.add_argument("--list-sessions", action="store_true", help="one row per session in the period")
    ap.add_argument("--today", help=argparse.SUPPRESS)  # fixed date for reproducible examples and tests
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    projects = args.projects or default_projects()
    today = args.today or dt.date.today().isoformat()
    since = dt.datetime.now() - dt.timedelta(days=args.days)

    if args.session is not None or args.list_sessions:
        runs = collect(projects, since)
        md, rows = session_report(runs, args.session, args.days)
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False, indent=1)
        write_out(md, args.out)
        return 0

    agents_def, skills_def = load_definitions(args.repo)
    runs = collect(projects, since, args.agent)
    if not runs:
        print(f"No runs in the last {args.days} days under {projects}.")
        return 0
    by_agent, reads, skill_use, tool_results = analyse(runs, skills_def)
    props = findings(by_agent, reads, agents_def, skills_def)
    md = report(runs, by_agent, reads, skill_use, tool_results, props, agents_def, skills_def, args.days, args.top, today)
    if args.json:
        data = {name: {k: (dict(v) if isinstance(v, collections.Counter) else v)
                       for k, v in a.items() if k not in ("startups", "sys_chars")}
                for name, a in by_agent.items()}
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"agents": data, "proposals": props}, f, ensure_ascii=False, indent=1, default=str)
    write_out(md, args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
