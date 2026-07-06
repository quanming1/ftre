"""刷库脚本：把旧事件格式的 messages 表迁移为新消息格式。

用法：
    python scripts/migrate_messages.py

会自动备份原表为 messages_old，然后原地替换。
"""
import asyncio
import json
import logging
import os
import sys
import time
import uuid

# 让脚本能直接运行
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from ftre.session.migrate_events import _coalesce_session_rows

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

DB_PATH = os.path.expanduser("~/.ftre/sessions.db")


async def migrate():
    import aiosqlite

    if not os.path.exists(DB_PATH):
        logger.error(f"DB 不存在: {DB_PATH}")
        return

    logger.info(f"开始迁移: {DB_PATH} ({os.path.getsize(DB_PATH) / 1024 / 1024:.1f} MB)")

    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row

    # 1. 检测是否需要迁移
    cursor = await db.execute("""
        SELECT COUNT(*) as cnt FROM messages
        WHERE type IN ('usage_update', 'reasoning_complete', 'tool_call')
    """)
    count = (await cursor.fetchone())["cnt"]

    if count == 0:
        logger.info("无旧事件数据，无需迁移")
        await db.close()
        return

    logger.info(f"检测到 {count} 行旧事件数据")

    # 2. 统计旧数据分布
    cursor = await db.execute("""
        SELECT type, COUNT(*) as cnt FROM messages GROUP BY type ORDER BY cnt DESC
    """)
    type_dist = await cursor.fetchall()
    logger.info("当前 type 分布:")
    for row in type_dist:
        logger.info(f"  {row['type']:35s} {row['cnt']:>8}")

    # 3. 创建新表
    await db.executescript("""
        DROP TABLE IF EXISTS messages_new;
        CREATE TABLE messages_new (
            id          TEXT PRIMARY KEY,
            session_id  TEXT NOT NULL,
            type        TEXT NOT NULL,
            data        TEXT NOT NULL DEFAULT '{}',
            timestamp   REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_messages_new_session
            ON messages_new(session_id, timestamp ASC);
    """)
    await db.commit()

    # 4. 按 session 遍历迁移
    cursor = await db.execute("SELECT COUNT(DISTINCT session_id) as cnt FROM messages")
    total_sessions = (await cursor.fetchone())["cnt"]
    logger.info(f"共 {total_sessions} 个 session 需要处理")

    cursor = await db.execute("SELECT DISTINCT session_id FROM messages")
    session_ids = [r["session_id"] for r in await cursor.fetchall()]

    total_old = 0
    total_new = 0
    t0 = time.perf_counter()

    for si, sid in enumerate(session_ids):
        cursor = await db.execute(
            "SELECT id, session_id, type, data, timestamp FROM messages "
            "WHERE session_id = ? ORDER BY timestamp ASC",
            (sid,),
        )
        rows = await cursor.fetchall()
        total_old += len(rows)

        coalesced = _coalesce_session_rows(rows)

        # 批量插入
        insert_data = []
        for msg in coalesced:
            msg_id = uuid.uuid4().hex[:16]
            insert_data.append((
                msg_id,
                sid,
                msg["type"],
                json.dumps(msg["data"], ensure_ascii=False),
                msg["timestamp"],
            ))

        if insert_data:
            await db.executemany(
                "INSERT INTO messages_new (id, session_id, type, data, timestamp) VALUES (?, ?, ?, ?, ?)",
                insert_data,
            )
        total_new += len(insert_data)

        # 每 100 个 session 提交一次 + 打印进度
        if (si + 1) % 100 == 0 or si + 1 == len(session_ids):
            await db.commit()
            elapsed = time.perf_counter() - t0
            speed = (si + 1) / elapsed
            logger.info(
                f"  进度: {si + 1}/{len(session_ids)} sessions "
                f"({speed:.0f} sessions/s) | "
                f"旧 {total_old} → 新 {total_new} 行"
            )

    # 5. 统计新数据分布
    cursor = await db.execute("""
        SELECT type, COUNT(*) as cnt FROM messages_new GROUP BY type ORDER BY cnt DESC
    """)
    new_dist = await cursor.fetchall()
    logger.info("迁移后 type 分布:")
    for row in new_dist:
        logger.info(f"  {row['type']:35s} {row['cnt']:>8}")

    # 6. 替换表
    logger.info("替换 messages 表...")
    await db.executescript("""
        ALTER TABLE messages RENAME TO messages_old;
        ALTER TABLE messages_new RENAME TO messages;
    """)

    # 重建索引
    await db.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_session "
        "ON messages(session_id, timestamp ASC)"
    )
    await db.commit()

    elapsed = time.perf_counter() - t0
    logger.info(
        f"迁移完成: {total_old} 行旧事件 → {total_new} 行新消息 "
        f"({len(session_ids)} sessions, {elapsed:.1f}s)"
    )
    logger.info(f"旧表已保留为 messages_old（可手动删除）")

    await db.close()


if __name__ == "__main__":
    asyncio.run(migrate())
