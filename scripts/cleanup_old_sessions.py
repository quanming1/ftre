"""清理 7 天前的 session 数据，减小数据库体积。

步骤：
1. 删除残留的 messages_new 表（之前迁移中断留下）
2. 删除 messages 表中 timestamp < cutoff 的行
3. 删除 sessions 表中已无 messages 引用的 session
4. 删除 external_sessions 表中已无 messages 引用的 session
5. VACUUM 回收磁盘空间

用法：
    python scripts/cleanup_old_sessions.py [--days 7]
"""
import sqlite3
import os
import sys
import time
from datetime import datetime

DAYS = int(sys.argv[sys.argv.index("--days") + 1]) if "--days" in sys.argv else 7
DB_PATH = os.path.expanduser("~/.ftre/sessions.db")

print(f"DB: {DB_PATH}")
print(f"DB size: {os.path.getsize(DB_PATH) / 1024 / 1024:.1f} MB")
print(f"Cutoff: {DAYS} days")

db = sqlite3.connect(DB_PATH, timeout=60)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=30000")

cutoff = time.time() - DAYS * 86400
cutoff_dt = datetime.fromtimestamp(cutoff)
print(f"Cutoff datetime: {cutoff_dt}")
print()

# ── 1. 删除残留的 messages_new 表 ──
print("=== Step 1: Drop stale messages_new ===")
cursor = db.execute(
    "SELECT name FROM sqlite_master WHERE type='table' AND name='messages_new'"
)
if cursor.fetchone():
    cursor = db.execute("SELECT COUNT(*) as cnt FROM messages_new")
    cnt = cursor.fetchone()["cnt"]
    print(f"  messages_new exists with {cnt} rows (partial migration leftover)")
    db.execute("DROP TABLE messages_new")
    db.commit()
    print("  Dropped messages_new")
else:
    print("  messages_new does not exist, skip")

# ── 2. 统计要删除的数据 ──
print("\n=== Step 2: Analyze data to delete ===")
cursor = db.execute(
    "SELECT COUNT(*) as cnt FROM messages WHERE timestamp < ?", (cutoff,)
)
old_msg_rows = cursor.fetchone()["cnt"]
print(f"  Messages older than cutoff: {old_msg_rows}")

cursor = db.execute(
    "SELECT COUNT(DISTINCT session_id) as cnt FROM messages WHERE timestamp < ?",
    (cutoff,),
)
old_sessions_with_msgs = cursor.fetchone()["cnt"]
print(f"  Sessions with old messages: {old_sessions_with_msgs}")

# 找到完全在 cutoff 之前的 session（所有消息都 < cutoff）
cursor = db.execute("""
    SELECT DISTINCT session_id FROM messages
    WHERE timestamp < ?
    AND session_id NOT IN (
        SELECT DISTINCT session_id FROM messages WHERE timestamp >= ?
    )
""", (cutoff, cutoff))
fully_old_sessions = [r["session_id"] for r in cursor.fetchall()]
print(f"  Sessions fully older than cutoff (to delete): {len(fully_old_sessions)}")

# ── 3. 删除完全在 cutoff 之前的 session 的 messages ──
print(f"\n=== Step 3: Delete old session messages ===")
if fully_old_sessions:
    # 分批删除，每批 500 个 session
    batch_size = 500
    total_deleted = 0
    for i in range(0, len(fully_old_sessions), batch_size):
        batch = fully_old_sessions[i : i + batch_size]
        placeholders = ",".join("?" * len(batch))
        cursor = db.execute(
            f"DELETE FROM messages WHERE session_id IN ({placeholders})",
            batch,
        )
        total_deleted += cursor.rowcount
        db.commit()
        print(f"  Batch {i // batch_size + 1}: deleted {cursor.rowcount} rows "
              f"(total {total_deleted})")
    print(f"  Total messages deleted: {total_deleted}")

# ── 4. 删除 sessions 表中的孤立行 ──
print(f"\n=== Step 4: Delete orphaned sessions ===")
if fully_old_sessions:
    batch_size = 500
    total_deleted = 0
    for i in range(0, len(fully_old_sessions), batch_size):
        batch = fully_old_sessions[i : i + batch_size]
        placeholders = ",".join("?" * len(batch))
        cursor = db.execute(
            f"DELETE FROM sessions WHERE id IN ({placeholders})",
            batch,
        )
        total_deleted += cursor.rowcount
        db.commit()
    print(f"  Sessions deleted: {total_deleted}")

# ── 5. 删除 external_sessions 表中的孤立行 ──
print(f"\n=== Step 5: Delete orphaned external_sessions ===")
if fully_old_sessions:
    batch_size = 500
    total_deleted = 0
    for i in range(0, len(fully_old_sessions), batch_size):
        batch = fully_old_sessions[i : i + batch_size]
        placeholders = ",".join("?" * len(batch))
        cursor = db.execute(
            f"DELETE FROM external_sessions WHERE session_id IN ({placeholders})",
            batch,
        )
        total_deleted += cursor.rowcount
        db.commit()
    print(f"  External sessions deleted: {total_deleted}")

# ── 6. VACUUM ──
print(f"\n=== Step 6: VACUUM ===")
print("  VACUUMing... (this may take a while)")
db.execute("VACUUM")
db.commit()
print("  VACUUM done")

# ── 7. 统计结果 ──
print(f"\n=== Result ===")
print(f"DB size: {os.path.getsize(DB_PATH) / 1024 / 1024:.1f} MB")

cursor = db.execute("SELECT COUNT(*) as cnt FROM messages")
print(f"Total messages: {cursor.fetchone()['cnt']}")

cursor = db.execute("SELECT COUNT(*) as cnt FROM sessions")
print(f"Total sessions: {cursor.fetchone()['cnt']}")

cursor = db.execute("SELECT COUNT(*) as cnt FROM external_sessions")
print(f"Total external_sessions: {cursor.fetchone()['cnt']}")

# type 分布
print("\n--- messages type 分布 ---")
cursor = db.execute(
    "SELECT type, COUNT(*) as cnt FROM messages GROUP BY type ORDER BY cnt DESC"
)
for r in cursor.fetchall():
    print(f"  {r['type']:35s} {r['cnt']:>8}")

db.close()
print("\nDone!")
