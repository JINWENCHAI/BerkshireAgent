"""Dump reasoning_content and additional_kwargs in full for QQ thread."""

import json
import sqlite3

import ormsgpack
import langgraph.checkpoint.serde.jsonplus as _jp

_msgpack_ext_hook = _jp._msgpack_ext_hook

DB = r"C:\Users\13281\Documents\BerkshireAgent\backend\.berkshire-agent\data\deerflow.db"
conn = sqlite3.connect(DB)
cur = conn.cursor()

cur.execute(
    "SELECT checkpoint, metadata FROM checkpoints "
    "WHERE thread_id=? ORDER BY checkpoint_id DESC LIMIT 1",
    ("09f79600-2bed-4f0f-a03f-3b2990088464",),
)
ckpt_blob, _ = cur.fetchone()
ckpt = ormsgpack.unpackb(
    ckpt_blob, ext_hook=_msgpack_ext_hook, option=ormsgpack.OPT_NON_STR_KEYS
)

msgs = ckpt.get("channel_values", {}).get("messages", [])
for i, m in enumerate(msgs):
    extra = getattr(m, "additional_kwargs", None) or {}
    print(f"\n[{i}] {type(m).__name__} name={getattr(m, 'name', None)!r}")
    if "reasoning_content" in extra:
        rc = extra["reasoning_content"]
        print(f"   reasoning_content ({len(rc)} chars):")
        print("   " + rc.replace("\n", "\n   "))
    if "token_usage_attribution" in extra:
        tua = extra["token_usage_attribution"]
        print(f"   token_usage_attribution: {json.dumps(tua, indent=2, ensure_ascii=False)[:800]}")
    if "deerflow_tool_meta" in extra:
        print(f"   deerflow_tool_meta: {json.dumps(extra['deerflow_tool_meta'], indent=2, ensure_ascii=False)}")
    if "deerflow_tool_receipt" in extra:
        print(f"   deerflow_tool_receipt: {json.dumps(extra['deerflow_tool_receipt'], indent=2, ensure_ascii=False)}")
