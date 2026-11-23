"""backfill_embeddings: re-embed facts stored without an embedding (t_4b0e7956).

Hermetic: a scratch SQLite table with the SDK's column names and a fake
embedder, so neither the memori SDK nor the live memory DB is touched.
"""

from __future__ import annotations

import json
import sqlite3
import struct

from memori_byodb import backfill_embeddings as bf

DIM = 3


def _db(rows):
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE memori_entity_fact (id INTEGER PRIMARY KEY, content TEXT, content_embedding BLOB)")
    conn.executemany("INSERT INTO memori_entity_fact VALUES (?, ?, ?)", rows)
    return conn


def _embed(texts, model):
    return [[float(len(t)), 1.0, 2.0] for t in texts]


def _vec(n):
    return struct.pack(f"<{n}f", *([0.5] * n))


def test_report_only_counts_empty_and_wrong_dimension_rows():
    conn = _db([(1, "a", b""), (2, "bb", _vec(DIM)), (3, "ccc", _vec(768)), (4, "d", None)])
    report = bf.backfill(conn, _embed, "m", apply=False)
    assert report == {"facts": 4, "to_embed": 3, "dimension": DIM, "model": "m", "applied": False}
    assert conn.execute("SELECT content_embedding FROM memori_entity_fact WHERE id=1").fetchone()[0] == b""


def test_apply_writes_sdk_format_and_leaves_good_rows():
    good = _vec(DIM)
    conn = _db([(1, "a", b""), (2, "bb", good), (3, "ccc", _vec(768))])
    report = bf.backfill(conn, _embed, "m", apply=True)
    assert report["embedded"] == 2
    rows = dict(conn.execute("SELECT id, content_embedding FROM memori_entity_fact").fetchall())
    assert rows[1] == struct.pack("<3f", 1.0, 1.0, 2.0)
    assert rows[2] == good
    assert len(rows[3]) == DIM * 4


def test_all_re_embeds_every_row():
    conn = _db([(1, "a", _vec(DIM)), (2, "bb", _vec(DIM))])
    assert bf.backfill(conn, _embed, "m", apply=True, all_rows=True)["embedded"] == 2


def test_short_embedding_result_raises_before_partial_garbage():
    conn = _db([(1, "a", b"")])
    calls = []

    def flaky(texts, model):
        calls.append(texts)
        return [[1.0, 2.0, 3.0]] if len(calls) == 1 else []

    try:
        bf.backfill(conn, flaky, "m", apply=True)
    except RuntimeError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected RuntimeError")
    assert conn.execute("SELECT content_embedding FROM memori_entity_fact").fetchone()[0] == b""


def test_resolve_db_prefers_flag_then_env_then_config(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HSCC_MEMORI_DB_PATH", raising=False)
    assert bf.resolve_db(None) == tmp_path / "memori_byodb.db"
    (tmp_path / "memori_byodb.json").write_text(json.dumps({"dbPath": str(tmp_path / "cfg.db")}))
    assert bf.resolve_db(None) == tmp_path / "cfg.db"
    monkeypatch.setenv("HSCC_MEMORI_DB_PATH", str(tmp_path / "env.db"))
    assert bf.resolve_db(None) == tmp_path / "env.db"
    assert bf.resolve_db(str(tmp_path / "flag.db")) == tmp_path / "flag.db"
