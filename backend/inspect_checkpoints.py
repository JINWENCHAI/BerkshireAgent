"""Decode the latest LangGraph checkpoint from SQLite (msgpack checkpoint blob + json metadata).

Usage:  python backend/inspect_checkpoints.py
        python backend/inspect_checkpoints.py --all         # walk every thread, write JSON to .berkshire-agent/dumps/
"""

import json
import os
import sqlite3
import sys

import ormsgpack

# Force UTF-8 output regardless of Windows console encoding
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DB = r"C:\Users\13281\Documents\BerkshireAgent\backend\.berkshire-agent\data\deerflow.db"

import langgraph.checkpoint.serde.jsonplus as _jp
_msgpack_ext_hook = _jp._msgpack_ext_hook


def _msg_to_dict(m):
    """Convert a LangChain BaseMessage into a JSON-friendly dict."""
    if m is None:
        return None
    out = {"class": type(m).__name__}
    for attr in ("type", "name", "id"):
        if hasattr(m, attr):
            v = getattr(m, attr)
            if v is not None:
                out[attr] = v
    content = getattr(m, "content", None)
    out["content"] = content if isinstance(content, (str, list, dict)) else repr(content)
    tool_calls = getattr(m, "tool_calls", None) or []
    if tool_calls:
        out["tool_calls"] = [
            {k: v for k, v in tc.items() if k in ("name", "args", "id")} for tc in tool_calls
        ]
    extra = getattr(m, "additional_kwargs", None) or {}
    if extra:
        # keep only type/tag fields, not the full payload
        out["additional_keys"] = list(extra.keys())
    return out


def _decode_thread(conn, thread_id, limit=10):
    """Decode up to ``limit`` most recent checkpoints for a thread."""
    cur = conn.cursor()
    cur.execute(
        "SELECT checkpoint_id, parent_checkpoint_id, length(checkpoint), metadata, checkpoint "
        "FROM checkpoints WHERE thread_id=? ORDER BY checkpoint_id DESC LIMIT ?",
        (thread_id, limit),
    )
    out = []
    for row in cur.fetchall():
        cid, parent, size, meta_blob, ckpt_blob = row
        meta = json.loads(meta_blob)
        ckpt = ormsgpack.unpackb(
            ckpt_blob, ext_hook=_msgpack_ext_hook, option=ormsgpack.OPT_NON_STR_KEYS
        )
        ch_vals = ckpt.get("channel_values") or {}
        msgs = ch_vals.get("messages") if isinstance(ch_vals, dict) else None
        out.append(
            {
                "checkpoint_id": cid,
                "parent_checkpoint_id": parent,
                "step": meta.get("step"),
                "ts": ckpt.get("ts"),
                "messages": [_msg_to_dict(m) for m in (msgs or [])],
                "channel_value_keys": list(ch_vals.keys()) if isinstance(ch_vals, dict) else [],
                "metadata": meta,
            }
        )
    return out


def main():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row

    print(f"db = {DB}\n")

    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM checkpoints")
    print(f"total checkpoints: {cur.fetchone()[0]}")

    cur.execute(
        "SELECT thread_id, COUNT(*) AS n, MAX(checkpoint_id) AS latest "
        "FROM checkpoints GROUP BY thread_id ORDER BY latest DESC"
    )
    threads = cur.fetchall()
    print(f"distinct threads: {len(threads)}\n")
    for t in threads:
        print(f"  thread={t['thread_id']}  checkpoints={t['n']}  latest={t['latest']}")

    print()
    for t in threads:
        tid = t["thread_id"]
        print(f"\n=== thread {tid} (latest 3 checkpoints) ===")
        for i, snap in enumerate(_decode_thread(conn, tid, limit=3)):
            print(f"\n  [cp {i}] {snap['checkpoint_id']}  step={snap['step']}  ts={snap['ts']}")
            print(f"        parent = {snap['parent_checkpoint_id']}")
            print(f"        channel_value_keys = {snap['channel_value_keys']}")
            print(f"        messages ({len(snap['messages'])}):")
            for j, m in enumerate(snap["messages"]):
                content = m.get("content")
                if isinstance(content, str):
                    snippet = content[:200].replace("\n", " ")
                else:
                    snippet = repr(content)[:200]
                print(f"          [{j}] {m['class']} type={m.get('type')!r} id={m.get('id')}")
                if m.get("name"):
                    print(f"              name={m['name']!r}")
                print(f"              content: {snippet}")
                if m.get("tool_calls"):
                    print(f"              tool_calls: {[t.get('name') for t in m['tool_calls']]}")


if __name__ == "__main__":
    main()
