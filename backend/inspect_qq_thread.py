"""Show the reasoning_content from the AIMessage that succeeded."""

import json
import sqlite3

import ormsgpack
import langgraph.checkpoint.serde.jsonplus as _jp

_msgpack_ext_hook = _jp._msgpack_ext_hook

DB = r"C:\Users\13281\Documents\BerkshireAgent\backend\.berkshire-agent\data\deerflow.db"
conn = sqlite3.connect(DB)
cur = conn.cursor()

# 取最新一条 checkpoint
cur.execute(
    "SELECT checkpoint, metadata FROM checkpoints "
    "WHERE thread_id=? ORDER BY checkpoint_id DESC LIMIT 1",
    ("09f79600-2bed-4f0f-a03f-3b2990088464",),
)
ckpt_blob, meta_blob = cur.fetchone()
ckpt = ormsgpack.unpackb(
    ckpt_blob, ext_hook=_msgpack_ext_hook, option=ormsgpack.OPT_NON_STR_KEYS
)
meta = json.loads(meta_blob)

print("=== metadata ===")
for k, v in meta.items():
    if isinstance(v, (str, int, float, bool)):
        print(f"  {k} = {v}")
    else:
        print(f"  {k} = <{type(v).__name__}>")

print("\n=== messages ===")
msgs = ckpt.get("channel_values", {}).get("messages", [])
for i, m in enumerate(msgs):
    content = getattr(m, "content", None)
    extra = getattr(m, "additional_kwargs", None) or {}
    print(f"\n[{i}] {type(m).__name__} type={getattr(m, 'type', None)!r} name={getattr(m, 'name', None)!r}")
    if isinstance(content, str):
        print(f"    content: {content[:500]}")
    elif isinstance(content, list):
        for blk in content:
            print(f"    block: {repr(blk)[:300]}")
    if extra:
        print(f"    additional keys: {list(extra.keys())}")
        for k, v in extra.items():
            if isinstance(v, str):
                print(f"      {k}: {v[:300]}")
            elif isinstance(v, dict):
                print(f"      {k}: <dict> {list(v.keys())}")
            else:
                print(f"      {k}: <{type(v).__name__}> {repr(v)[:200]}")
