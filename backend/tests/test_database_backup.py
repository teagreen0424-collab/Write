"""数据库交付工具：只读在线备份、WAL 一致性和失败清理。"""
from contextlib import closing, redirect_stderr, redirect_stdout
import importlib.util
import io
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


script = Path(__file__).resolve().parents[2] / "scripts" / "backup_database.py"
spec = importlib.util.spec_from_file_location("lingmo_backup_database", script)
backup_tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup_tool)


class DatabaseBackupTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory(prefix="lingmo-backup-test-")
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.source = self.directory / "source.db"
        self.output = self.directory / "backups"

    def create_source(self, path=None):
        path = path or self.source
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE chapter (content TEXT)")
            connection.execute("INSERT INTO chapter VALUES (?)", ("已保存正文",))
            connection.commit()
        return path

    def test_online_backup_includes_committed_wal_rows_and_preserves_source(self):
        with closing(sqlite3.connect(self.source)) as live:
            self.assertEqual(live.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
            live.execute("PRAGMA wal_autocheckpoint=0")
            live.execute("CREATE TABLE chapter (content TEXT)")
            live.execute("INSERT INTO chapter VALUES (?)", ("WAL 内已提交正文",))
            live.commit()
            self.assertTrue(Path(str(self.source) + "-wal").is_file())
            original_bytes = self.source.read_bytes()
            backup = backup_tool.backup_database(self.source, self.output)
            with closing(sqlite3.connect(backup)) as copied:
                self.assertEqual(copied.execute("SELECT content FROM chapter").fetchall(), [("WAL 内已提交正文",)])
                self.assertEqual(copied.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            self.assertEqual(self.source.read_bytes(), original_bytes)
            self.assertEqual(live.execute("SELECT count(*) FROM chapter").fetchone()[0], 1)

    def test_missing_source_fails_without_creating_source_or_output(self):
        with self.assertRaises(FileNotFoundError):
            backup_tool.backup_database(self.source, self.output)
        self.assertFalse(self.source.exists())
        self.assertFalse(self.output.exists())

    def test_exclusive_destination_never_overwrites_existing_backup(self):
        self.create_source()
        self.output.mkdir()
        existing = self.output / "existing.db"
        existing.write_bytes(b"do not overwrite")
        with patch.object(backup_tool, "_backup_name", return_value=existing.name):
            with self.assertRaises(FileExistsError):
                backup_tool.backup_database(self.source, self.output)
        self.assertEqual(existing.read_bytes(), b"do not overwrite")
        self.assertEqual(list(self.output.iterdir()), [existing])

    def test_chinese_and_space_paths_work_and_repeated_backups_are_unique(self):
        self.source = self.create_source(self.directory / "作品 正文.db")
        self.output = self.directory / "备份 目录"
        first = backup_tool.backup_database(self.source, self.output)
        second = backup_tool.backup_database(self.source, self.output)
        self.assertNotEqual(first, second)
        for path in (first, second):
            with closing(sqlite3.connect(path)) as copied:
                self.assertEqual(copied.execute("SELECT content FROM chapter").fetchone()[0], "已保存正文")

    def test_integrity_failure_removes_only_new_file_and_cli_never_claims_success(self):
        self.create_source()
        self.output.mkdir()
        existing = self.output / "prior.db"
        existing.write_bytes(b"keep")
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(backup_tool, "_verify_backup", side_effect=sqlite3.DatabaseError("invalid backup")):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = backup_tool.main(["--source", str(self.source), "--output-dir", str(self.output)])
        self.assertEqual(code, 1)
        self.assertNotIn("[OK]", stdout.getvalue())
        self.assertIn("[ERROR]", stderr.getvalue())
        self.assertEqual(list(self.output.iterdir()), [existing])
        self.assertEqual(existing.read_bytes(), b"keep")

    def test_invalid_source_database_is_rejected_before_creating_output(self):
        self.source.write_bytes(b"not sqlite")
        with self.assertRaises(sqlite3.DatabaseError):
            backup_tool.backup_database(self.source, self.output)
        self.assertFalse(self.output.exists())

    def test_source_connection_cannot_write_database(self):
        self.create_source()
        actual_connect = sqlite3.connect
        source_was_readonly = []

        def guarded_connect(database, *args, **kwargs):
            connection = actual_connect(database, *args, **kwargs)
            if not source_was_readonly:
                try:
                    with self.assertRaises(sqlite3.OperationalError):
                        connection.execute("INSERT INTO chapter VALUES ('should not be writable')")
                    source_was_readonly.append(True)
                except BaseException:
                    connection.close()
                    raise
            return connection

        with patch.object(backup_tool.sqlite3, "connect", side_effect=guarded_connect):
            backup_tool.backup_database(self.source, self.output)
        self.assertEqual(source_was_readonly, [True])
        with closing(actual_connect(self.source)) as original:
            self.assertEqual(original.execute("SELECT count(*) FROM chapter").fetchone()[0], 1)

    def test_backup_timeout_removes_new_output_without_touching_source(self):
        self.create_source()
        with patch.object(backup_tool.time, "monotonic", side_effect=[0, 31]):
            with self.assertRaises(TimeoutError):
                backup_tool.backup_database(self.source, self.output)
        self.assertEqual(list(self.output.iterdir()), [])
        with closing(sqlite3.connect(self.source)) as original:
            self.assertEqual(original.execute("SELECT count(*) FROM chapter").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
