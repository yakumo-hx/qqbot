# -*- coding: utf-8 -*-
"""比较同一事件在两条反向 WS 通道上的字段差异（A1 双通道一致性）。"""
import json
from collections import defaultdict

path = r"E:\QQbot\qq_native_re\findings\llbot_trial\logs\stageA\probe\events.jsonl"
rows = [json.loads(line) for line in open(path, encoding="utf-8") if line.strip()]
msgs = [r for r in rows if r.get("post_type") == "message"]
by_mid = defaultdict(dict)
for r in msgs:
    by_mid[str(r.get("message_id"))][r["channel"]] = r
print(f"message events: {len(msgs)}, distinct message_id: {len(by_mid)}")
for mid, chans in by_mid.items():
    a, b = chans.get(0), chans.get(1)
    if not a or not b:
        continue
    keys = sorted(set(a) | set(b))
    diffs = [(k, a.get(k), b.get(k)) for k in keys if a.get(k) != b.get(k)]
    print(f"\nmid={mid} group={a.get('group_id')} ch0_bytes={a.get('raw_bytes')} ch1_bytes={b.get('raw_bytes')}")
    for k, va, vb in diffs:
        print(f"  DIFF {k}: ch0={str(va)[:120]!r} ch1={str(vb)[:120]!r}")
    sample_keys = ("message_format", "message_types", "message_summary", "sender_keys", "raw_message_len", "font")
    sample = {k: a.get(k) for k in sample_keys}
    print("  sample ch0:", json.dumps(sample, ensure_ascii=False)[:800])
