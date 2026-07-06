"""Inspect session messages"""
import sqlite3, json, os

db = sqlite3.connect(os.path.expanduser("~/.ftre/sessions.db"), timeout=30)
db.row_factory = sqlite3.Row

sid = "ws::sess_b767b337ac5c"
cursor = db.execute("SELECT id, type, data, timestamp FROM messages WHERE session_id = ? ORDER BY timestamp", (sid,))
rows = cursor.fetchall()
print(f"Session {sid}: {len(rows)} messages\n")

for i, r in enumerate(rows):
    data = json.loads(r["data"])
    content_preview = ""
    if "content" in data:
        if isinstance(data["content"], list):
            types = [b.get("type", "?") for b in data["content"] if isinstance(b, dict)]
            content_preview = f" content_types={types}"
        elif isinstance(data["content"], str):
            content_preview = f" content_str={data['content'][:30]!r}"
    print(f"{i:3d} {r['type']:30s} id={r['id'][:12]:12s} ts={r['timestamp']:.3f}{content_preview}")

db.close()
