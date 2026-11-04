#!/usr/bin/env python3
"""Write synthetic Claude Code session logs for a made-up project, so token_audit.py
can be tried and tested without anyone's real conversations.

  python examples/make_synthetic_logs.py OUT_DIR

creates OUT_DIR/projects/-srv-demo-shop/ with two sessions and their subagents, in
the same JSONL layout Claude Code writes (main thread `<session>.jsonl`, subagents
in `<session>/subagents/agent-*.jsonl` with a `.meta.json` naming the agent type,
`api-request-shape` records with the system prompt and tool definitions).

Every piece of text that would be private in real logs (prompts, tool output,
system prompt, replies) contains MARKER, so a test can prove the report never
carries it. Output is deterministic.
"""
import json
import os
import random
import sys

MARKER = "PRIVATE-CONTENT-MARKER"
CWD = "/srv/demo-shop"
PROJECT = "-srv-demo-shop"


def tool_def(name, size):
    desc = f"{name}: {MARKER} " + ("tool documentation " * (size // 19))
    return {"name": name, "description": desc[:size],
            "input_schema": {"type": "object", "properties": {"arg": {"type": "string"}}}}


TOOLS = {
    "main": [("Read", 1800), ("Edit", 1500), ("Grep", 1600), ("Bash", 2600), ("Agent", 3200), ("Skill", 900)],
    "code-reviewer": [("Read", 1800), ("Grep", 1600), ("Glob", 900), ("Bash", 2600), ("WebSearch", 1400)],
    "browser-checker": [("Read", 1800), ("Grep", 1600), ("WebFetch", 1500),
                        ("mcp__browser__navigate", 1200), ("mcp__browser__screenshot", 1100),
                        ("mcp__browser__click", 1300), ("mcp__browser__form_input", 1700),
                        ("mcp__browser__upload_file", 1600), ("mcp__browser__console_messages", 1400)],
    "docs-writer": [("Read", 1800), ("Edit", 1500), ("Write", 1400), ("Grep", 1600)],
}
SYSTEM_CHARS = {"main": 42000, "code-reviewer": 9000, "browser-checker": 12000, "docs-writer": 30000}
EFFORT = {"main": "medium", "code-reviewer": "high", "browser-checker": "medium", "docs-writer": "medium"}
MODEL = {"main": "claude-opus-5-5", "code-reviewer": "claude-opus-5-5",
         "browser-checker": "claude-sonnet-5-5", "docs-writer": "claude-sonnet-5-5"}


class Writer:
    """Builds one transcript: keeps a running context size so usage numbers look like real caching."""

    def __init__(self, rng, kind, session, ts, title=None):
        self.rng, self.kind, self.session = rng, kind, session
        self.lines, self.n, self.ts, self.tid = [], 0, ts, 0
        self.ctx = 0
        self.pending = 0
        if title:
            self.add({"type": "custom-title", "customTitle": title, "sessionId": session})
        system = (f"{MARKER} system prompt " * (SYSTEM_CHARS[kind] // 30))[:SYSTEM_CHARS[kind]]
        tools = [tool_def(n, s) for n, s in TOOLS[kind]]
        self.add({"type": "api-request-shape", "shapeHash": f"h-{kind}",
                  "shape": {"system": [{"type": "text", "text": system}], "tools": tools}})
        self.pending = (len(system) + sum(len(json.dumps(t)) for t in tools)) // 3.5
        self.user_text(f"{MARKER} please look at the checkout flow")

    def add(self, o):
        o.setdefault("cwd", CWD)
        o.setdefault("sessionId", self.session)
        o.setdefault("timestamp", f"{self.ts}:{len(self.lines) % 60:02d}Z")
        self.lines.append(json.dumps(o))

    def user_text(self, text):
        self.add({"type": "user", "message": {"role": "user", "content": text}})
        self.pending += len(text) // 3.5

    def request(self, tool=None, inp=None, split=False):
        """One API request; optionally ends in a tool_use. split=True writes the reply as two
        lines with the same message id + request id, as Claude Code does for streamed replies."""
        self.n += 1
        out = self.rng.randint(150, 900)
        usage = {"input_tokens": 3, "cache_creation_input_tokens": int(self.pending),
                 "cache_read_input_tokens": int(self.ctx), "output_tokens": out,
                 "cache_creation": {"ephemeral_1h_input_tokens": 0}}
        self.ctx += int(self.pending) + out
        self.pending = 0
        content = [{"type": "text", "text": f"{MARKER} thinking out loud"}]
        tid = None
        if tool:
            self.tid += 1
            tid = f"toolu_{self.session[:6]}_{self.kind[:4]}_{self.tid}"
            content.append({"type": "tool_use", "id": tid, "name": tool, "input": inp or {}})
        msg = {"id": f"msg_{self.session[:6]}_{self.kind}_{self.n}", "model": MODEL[self.kind],
               "role": "assistant", "content": content, "usage": usage}
        rid = f"req_{self.session[:6]}_{self.kind}_{self.n}"
        if split:
            first = dict(msg, content=content[:1])
            self.add({"type": "assistant", "requestId": rid, "message": first, "effort": EFFORT[self.kind]})
            self.add({"type": "assistant", "requestId": rid, "message": dict(msg, content=content[1:]), "effort": EFFORT[self.kind]})
        else:
            self.add({"type": "assistant", "requestId": rid, "message": msg, "effort": EFFORT[self.kind]})
        return tid

    def result(self, tid, chars=0, images=0, size=(1280, 800)):
        if images:
            content = [{"type": "text", "text": f"captured screenshot ({size[0]}x{size[1]}) {MARKER}"}]
            content += [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}] * images
            self.pending += images * size[0] * size[1] // 750
        else:
            content = (f"{MARKER} " + "file content line\n" * (chars // 18))[:chars]
            self.pending += chars // 3.5
        self.add({"type": "user", "message": {"role": "user",
                                              "content": [{"type": "tool_result", "tool_use_id": tid, "content": content}]}})

    def tool(self, name, inp=None, chars=400, images=0):
        self.result(self.request(name, inp), chars=chars, images=images)

    def save(self, path, meta=None):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write("\n".join(self.lines) + "\n")
        if meta:
            with open(path[:-6] + ".meta.json", "w", encoding="utf-8", newline="\n") as f:
                json.dump(meta, f)


def sub(rng, session, kind, idx, ts, steps):
    w = Writer(rng, kind, session, ts)
    for step in steps:
        w.tool(*step) if isinstance(step, tuple) else w.request()
    w.request()
    return w, {"agentType": kind, "isBuiltIn": False, "description": f"{kind} run {idx}"}


def build(out_dir):
    rng = random.Random(7)
    pdir = os.path.join(out_dir, "projects", PROJECT)
    spec = (f"{CWD}/docs/api-spec.md", 21000)

    # Session 1: a long main thread that rereads a big spec and spawns several subagents
    s1 = "11111111-aaaa-4bbb-8ccc-000000000001"
    w = Writer(rng, "main", s1, "2026-01-05T09:00", title="Checkout bug")
    w.tool("Read", {"file_path": spec[0]}, chars=spec[1])
    w.tool("Grep", {"pattern": "checkout"}, chars=1200)
    w.user_text(f"{MARKER} <command-name>/style-guide</command-name>")
    w.tool("Skill", {"skill": "release-notes"}, chars=300)
    for i in range(10):
        w.tool("Read", {"file_path": f"{CWD}/src/cart_{i % 4}.py"}, chars=3000)
        if i in (3, 7):
            w.tool("Read", {"file_path": spec[0]}, chars=spec[1])
        w.request(split=(i % 3 == 0))
    for i in range(8):
        w.tool("Agent", {"subagent_type": "code-reviewer"}, chars=800)
    w.save(os.path.join(pdir, s1 + ".jsonl"))

    subs = os.path.join(pdir, s1, "subagents")
    for i in range(3):
        sw, meta = sub(rng, s1, "code-reviewer", i, "2026-01-05T09:20", [("Read", {"file_path": f"{CWD}/src/cart_{i}.py"}, 3000), None])
        sw.save(os.path.join(subs, f"agent-cr{i}.jsonl"), meta)
    steps = [("mcp__browser__navigate", {}, 300)]
    for i in range(12):
        steps += [("mcp__browser__screenshot", {}, 0, 2), ("mcp__browser__click", {}, 200)]
    sw, meta = sub(rng, s1, "browser-checker", 0, "2026-01-05T09:40", steps)
    sw.save(os.path.join(subs, "agent-bc0.jsonl"), meta)
    sw, meta = sub(rng, s1, "docs-writer", 0, "2026-01-05T10:00",
                   [("Read", {"file_path": spec[0]}, spec[1]), ("Read", {"file_path": f"{CWD}/README.md"}, 6000),
                    None, ("Edit", {"file_path": f"{CWD}/README.md"}, 200), None, None])
    sw.save(os.path.join(subs, "agent-dw0.jsonl"), meta)

    # Session 2: shorter, one more browser run and two quick reviews
    s2 = "22222222-aaaa-4bbb-8ccc-000000000002"
    w = Writer(rng, "main", s2, "2026-01-06T14:00", title="Pricing page copy")
    w.tool("Read", {"file_path": f"{CWD}/README.md"}, chars=6000)
    for i in range(6):
        w.tool("Read", {"file_path": f"{CWD}/src/pricing_{i % 2}.py"}, chars=2500)
        w.request()
    w.save(os.path.join(pdir, s2 + ".jsonl"))
    subs = os.path.join(pdir, s2, "subagents")
    for i in range(2):
        sw, meta = sub(rng, s2, "code-reviewer", i + 3, "2026-01-06T14:20", [None])
        sw.save(os.path.join(subs, f"agent-cr{i + 3}.jsonl"), meta)
    steps = [("mcp__browser__navigate", {}, 300)]
    for i in range(8):
        steps += [("mcp__browser__screenshot", {}, 0, 1)]
    sw, meta = sub(rng, s2, "browser-checker", 1, "2026-01-06T14:40", steps)
    sw.save(os.path.join(subs, "agent-bc1.jsonl"), meta)
    return os.path.join(out_dir, "projects")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python examples/make_synthetic_logs.py OUT_DIR")
    print(build(sys.argv[1]))
