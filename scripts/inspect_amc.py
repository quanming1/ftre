"""Inspect specific assistant_message_complete rows"""
import sqlite3, json, os

db = sqlite3.connect(os.path.expanduser("~/.ftre/sessions.db"), timeout=30)
db.row_factory = sqlite3.Row

sid = "ws::sess_b767b337ac5c"
ids = [
    "a66bedf57932",  # 13209
    "912e28de35cb",  # 13211
    "0d3cb898636a",  # 13213
    "4af7322adcc9",  # 13215
]

for row_id in ids:
    cursor = db.execute("SELECT id, type, data, timestamp FROM messages WHERE session_id = ? AND id = ?", (sid, row_id))
    r = cursor.fetchone()
    if not r:
        print(f"Not found: {row_id}")
        continue
    data = json.loads(r["data"])
    content = data.get("content", [])
    print(f"\n=== {row_id} ts={r['timestamp']:.3f} ===")
    for i, block in enumerate(content):
        if isinstance(block, dict):
            t = block.get("type")
            if t == "toolCall":
                print(f"  [{i}] toolCall: {block.get('name')}({block.get('id')}) args={str(block.get('arguments', {}))[:60]}")
            elif t == "text":
                print(f"  [{i}] text: {block.get('text', '')[:80]!r}")
            elif t == "thinking":
                print(f"  [{i}] thinking: {block.get('thinking', '')[:80]!r}")
            else:
                print(f"  [{i}] {t}: {str(block)[:80]}")

db.close()
