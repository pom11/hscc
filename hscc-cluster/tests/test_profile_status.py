"""Tests for profile_status — per-profile kanban task counting."""
import os
import sqlite3
import tempfile

import profile_status


class TestProfileStatusMissingDb:
    """Missing DB returns empty dict, never raises."""

    def test_missing_db_returns_empty(self):
        result = profile_status.get_profile_task_counts("/nonexistent/path/kanban.db")
        assert result == {}

    def test_missing_db_status_returns_empty_struct(self):
        result = profile_status.get_profile_status("/nonexistent/path/kanban.db")
        assert result["counts"] == {}
        assert result["total_running"] == 0
        assert result["profiles"] == []


class TestProfileStatusWithRunningTasks:
    """Mock DB rows and verify counts."""

    def _create_mock_db(self):
        """Create an in-memory DB with the kanban tasks schema."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE tasks (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                body TEXT,
                assignee TEXT,
                status TEXT NOT NULL DEFAULT 'ready',
                priority INTEGER DEFAULT 5,
                created_by TEXT,
                created_at INTEGER DEFAULT 0,
                started_at INTEGER,
                completed_at INTEGER
            )
        """)
        conn.commit()
        conn.close()
        return path

    def test_no_running_tasks(self):
        db_path = self._create_mock_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO tasks (id, title, status, assignee) "
                "VALUES ('t1', 'done task', 'done', 'worker')"
            )
            conn.commit()
            conn.close()

            result = profile_status.get_profile_task_counts(db_path)
            assert result == {}
        finally:
            os.unlink(db_path)

    def test_single_running_task(self):
        db_path = self._create_mock_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.execute(
                "INSERT INTO tasks (id, title, status, assignee) "
                "VALUES ('t1', 'work in progress', 'running', 'devops-engineer')"
            )
            conn.commit()
            conn.close()

            result = profile_status.get_profile_task_counts(db_path)
            assert result == {"devops-engineer": 1}
        finally:
            os.unlink(db_path)

    def test_multiple_profiles(self):
        db_path = self._create_mock_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.executemany(
                "INSERT INTO tasks (id, title, status, assignee) VALUES (?, ?, ?, ?)",
                [
                    ("t1", "task 1", "running", "devops-engineer"),
                    ("t2", "task 2", "running", "devops-engineer"),
                    ("t3", "task 3", "running", "worker"),
                    ("t4", "task 4", "done", "worker"),
                    ("t5", "task 5", "ready", "coder"),
                ],
            )
            conn.commit()
            conn.close()

            result = profile_status.get_profile_task_counts(db_path)
            assert result == {"devops-engineer": 2, "worker": 1}
        finally:
            os.unlink(db_path)

    def test_profile_status_aggregation(self):
        db_path = self._create_mock_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.executemany(
                "INSERT INTO tasks (id, title, status, assignee) VALUES (?, ?, ?, ?)",
                [
                    ("t1", "task 1", "running", "devops-engineer"),
                    ("t2", "task 2", "running", "worker"),
                    ("t3", "task 3", "running", "worker"),
                ],
            )
            conn.commit()
            conn.close()

            result = profile_status.get_profile_status(db_path)
            assert result["counts"] == {"devops-engineer": 1, "worker": 2}
            assert result["total_running"] == 3
            assert result["profiles"] == ["devops-engineer", "worker"]
        finally:
            os.unlink(db_path)

    def test_null_assignee_ignored(self):
        db_path = self._create_mock_db()
        try:
            conn = sqlite3.connect(db_path)
            conn.executemany(
                "INSERT INTO tasks (id, title, status, assignee) VALUES (?, ?, ?, ?)",
                [
                    ("t1", "task 1", "running", None),
                    ("t2", "task 2", "running", ""),
                    ("t3", "task 3", "running", "worker"),
                ],
            )
            conn.commit()
            conn.close()

            result = profile_status.get_profile_task_counts(db_path)
            assert result == {"worker": 1}
        finally:
            os.unlink(db_path)

    def test_corrupt_db_returns_empty(self):
        """Corrupt file returns empty dict, never raises."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        with open(path, "wb") as f:
            f.write(b"not a sqlite database")

        try:
            result = profile_status.get_profile_task_counts(path)
            assert result == {}
        finally:
            os.unlink(path)

    def test_db_without_tasks_table(self):
        """DB with no tasks table returns empty dict."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE other (id TEXT)")
        conn.commit()
        conn.close()

        try:
            result = profile_status.get_profile_task_counts(path)
            assert result == {}
        finally:
            os.unlink(path)