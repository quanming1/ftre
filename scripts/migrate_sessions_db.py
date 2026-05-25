#!/usr/bin/env python3
"""
sessions.db 路径迁移脚本

老版默认路径：<工作目录>/data/sessions.db
新版默认路径：~/.ftre/sessions.db（与 config.json 同目录）

用法：
    python scripts/migrate_sessions_db.py            # 干跑：仅检查并打印结果，不写入
    python scripts/migrate_sessions_db.py --apply    # 实际执行复制
    python scripts/migrate_sessions_db.py --apply --force
                                                     # 即便目标已存在也强制覆盖（先备份为 .bak）
    python scripts/migrate_sessions_db.py --src PATH --dst PATH [--apply]
                                                     # 自定义源/目标路径

退出码：
    0  无需迁移 / 迁移成功
    1  发生错误（参数非法、源不存在、IO 失败等）
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path


def default_legacy_path() -> Path:
    """老版默认路径（相对当前工作目录）"""
    return Path("./data/sessions.db").resolve()


def default_new_path() -> Path:
    """新版默认路径：~/.ftre/sessions.db；Windows 下走 USERPROFILE"""
    if sys.platform == "win32":
        home = Path(os.environ.get("USERPROFILE", str(Path.home())))
    else:
        home = Path.home()
    return home / ".ftre" / "sessions.db"


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} B"
        n /= 1024
    return f"{n:.1f} TB"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--src", type=Path, default=None, help="源路径（默认 ./data/sessions.db）")
    p.add_argument("--dst", type=Path, default=None, help="目标路径（默认 ~/.ftre/sessions.db）")
    p.add_argument("--apply", action="store_true", help="实际执行；不加则只做 dry run")
    p.add_argument(
        "--force",
        action="store_true",
        help="目标已存在时也覆盖；覆盖前会先备份为 <dst>.bak.<时间戳>",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()

    src = (args.src or default_legacy_path()).resolve()
    dst = (args.dst or default_new_path()).resolve()
    dry_run = not args.apply

    print(f"源:   {src}")
    print(f"目标: {dst}")
    print(f"模式: {'DRY-RUN（不会写入）' if dry_run else 'APPLY'}")
    print()

    if not src.is_file():
        print("[skip] 源文件不存在，无需迁移")
        return 0

    src_size = src.stat().st_size
    print(f"[info] 源文件大小: {human_size(src_size)}")

    if dst.exists():
        if not args.force:
            print(
                f"[abort] 目标已存在: {dst}\n"
                "        - 加 --force 可覆盖（覆盖前会先备份）\n"
                "        - 或加 --dst PATH 指定其它目标"
            )
            return 1
        backup = dst.with_suffix(dst.suffix + f".bak.{int(time.time())}")
        print(f"[backup] 目标已存在，将先备份为: {backup}")
        if not dry_run:
            shutil.move(str(dst), str(backup))

    print(f"[copy] {src} → {dst}")
    if dry_run:
        print()
        print("dry run 完成。确认无误后用 --apply 执行实际复制。")
        return 0

    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(src), str(dst))
    except OSError as e:
        print(f"[error] 复制失败: {e}", file=sys.stderr)
        return 1

    new_size = dst.stat().st_size
    if new_size != src_size:
        print(
            f"[warn] 目标大小 ({human_size(new_size)}) 与源 ({human_size(src_size)}) 不一致",
            file=sys.stderr,
        )

    print()
    print("迁移完成。")
    print(f"  - 旧库保留在原位置作为备份: {src}")
    print(f"  - 新库: {dst}")
    print()
    print("下次启动 gateway 会自动使用新路径，确认无问题后可手动删除旧文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
