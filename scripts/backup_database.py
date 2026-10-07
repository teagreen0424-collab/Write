"""显式指定源库的 SQLite 一致性备份；不读取配置，也不覆盖已有备份。"""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3
import sys
import time
import uuid


def _backup_name(source: Path) -> str:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{source.stem}-{timestamp}-{uuid.uuid4().hex}.db"


def _verify_backup(connection: sqlite3.Connection) -> None:
    result = connection.execute("PRAGMA integrity_check").fetchall()
    if result != [("ok",)]:
        raise sqlite3.DatabaseError("Backup integrity check failed")


def backup_database(source: Path, output_dir: Path, *, timeout_seconds: float = 30) -> Path:
    source = Path(source).resolve(strict=True)
    if not source.is_file():
        raise ValueError("The source must be an existing SQLite database file")
    if timeout_seconds <= 0:
        raise ValueError("The backup timeout must be positive")

    # mode=ro 不会创建不存在的源库，并保留已提交 WAL 数据的可见性。
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=1)) as original:
        # 先验证 SQLite 能读取源库，失败时不创建输出文件或目录。
        original.execute("SELECT count(*) FROM sqlite_master").fetchone()
        output_dir = Path(output_dir).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        destination = output_dir / _backup_name(source)
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        try:
            os.close(descriptor)
            descriptor = None
            started = time.monotonic()

            def progress(_status: int, _remaining: int, _total: int) -> None:
                if time.monotonic() - started > timeout_seconds:
                    raise TimeoutError("SQLite backup did not complete within the time limit")

            with closing(sqlite3.connect(destination)) as backup:
                original.backup(backup, pages=256, progress=progress, sleep=0.05)
                _verify_backup(backup)
            return destination
        except BaseException:
            # 只清理由 O_EXCL 证明是本次创建的文件，不触碰既有备份和源库。
            if descriptor is not None:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
            destination.unlink(missing_ok=True)
            raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a verified SQLite online backup")
    parser.add_argument("--source", required=True, type=Path, help="Explicit existing source database")
    parser.add_argument("--output-dir", required=True, type=Path, help="Directory for a new unique backup")
    args = parser.parse_args(argv)
    try:
        destination = backup_database(args.source, args.output_dir)
    except (OSError, ValueError, sqlite3.Error, TimeoutError) as error:
        print(f"[ERROR] Backup was not created: {error}", file=sys.stderr)
        return 1
    print(f"[OK] Verified backup: {destination}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
