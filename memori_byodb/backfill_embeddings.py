"""Re-embed memori_entity_fact rows whose embedding is empty or of another model.

Until t_4b0e7956 the local augmentation stored every fact with no embedding, and
memori.recall refuses a whole query when any candidate row has none
(``missing_embedding_data``). This fills them in with the recall model so recall
works on the existing memory, not only on facts captured after the fix.

  python backfill_embeddings.py                 # report only
  python backfill_embeddings.py --apply         # back up the DB, then re-embed

The DB is the plugin's own: --db, else HSCC_MEMORI_DB_PATH, else dbPath in
~/.hermes/memori_byodb.json, else ~/.hermes/memori_byodb.db. Run it with the
Python that has the memori SDK (a Hermes environment).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sqlite3
import struct
import sys
from pathlib import Path
from typing import Callable

DEFAULT_MODEL = os.environ.get("HSCC_MEMORI_EMBED_MODEL", "all-MiniLM-L6-v2")
BATCH = 64


def resolve_db(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    if os.environ.get("HSCC_MEMORI_DB_PATH"):
        return Path(os.environ["HSCC_MEMORI_DB_PATH"]).expanduser()
    home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    cfg = home / "memori_byodb.json"
    try:
        raw = json.loads(cfg.read_text(encoding="utf-8"))
        path = raw.get("dbPath") or raw.get("db_path")
        if path:
            return Path(path).expanduser()
    except (OSError, ValueError):
        pass
    return home / "memori_byodb.db"


def stale_rows(conn: sqlite3.Connection, dim: int, *, all_rows: bool) -> list[tuple[int, str]]:
    """Rows to (re-)embed: an empty blob, or a vector of another dimension."""
    rows = conn.execute("SELECT id, content, content_embedding FROM memori_entity_fact").fetchall()
    out = []
    for row_id, content, blob in rows:
        size = len(blob) if blob else 0
        if all_rows or size != dim * 4:
            out.append((row_id, content or ""))
    return out


def pack(vector: list[float]) -> bytes:
    """The SDK's sqlite format (memori.embeddings.format_embedding_for_db)."""
    return struct.pack(f"<{len(vector)}f", *vector)


def backfill(conn: sqlite3.Connection, embed: Callable[[list[str], str], list[list[float]]],
             model: str, *, apply: bool, all_rows: bool = False) -> dict:
    dim = len(embed(["dimension probe"], model)[0])
    todo = stale_rows(conn, dim, all_rows=all_rows)
    total = conn.execute("SELECT count(*) FROM memori_entity_fact").fetchone()[0]
    report = {"facts": total, "to_embed": len(todo), "dimension": dim, "model": model, "applied": False}
    if not apply or not todo:
        return report
    done = 0
    for start in range(0, len(todo), BATCH):
        chunk = todo[start:start + BATCH]
        vectors = embed([content for _, content in chunk], model)
        if len(vectors) != len(chunk) or not all(vectors):
            raise RuntimeError(f"embedding returned {len(vectors)} vectors for {len(chunk)} facts")
        conn.executemany(
            "UPDATE memori_entity_fact SET content_embedding = ? WHERE id = ?",
            [(pack(vec), row_id) for (row_id, _), vec in zip(chunk, vectors)],
        )
        conn.commit()
        done += len(chunk)
    report.update(applied=True, embedded=done)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--db", help="SQLite DB (default: the plugin's configured DB)")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--all", action="store_true", help="re-embed every fact, not only stale ones")
    ap.add_argument("--apply", action="store_true", help="write (default: report only)")
    args = ap.parse_args(argv)

    db = resolve_db(args.db)
    if not db.is_file():
        print(json.dumps({"error": f"no memori DB at {db}"}))
        return 2
    from memori.embeddings import embed_texts

    def embed(texts: list[str], model: str) -> list[list[float]]:
        return embed_texts(texts, model=model)

    backup = None
    conn = sqlite3.connect(str(db), timeout=30)
    try:
        report = backfill(conn, embed, args.model, apply=False, all_rows=args.all)
        if args.apply and report["to_embed"]:
            # sqlite's online backup, not a file copy: the gateway may hold the
            # DB open, and a copied file would miss what still sits in its -wal.
            # Only when there is something to write, so a repeated run is cheap.
            backup = db.with_name(f"{db.name}.bak-{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}")
            with sqlite3.connect(str(backup)) as dest:
                conn.backup(dest)
            report = backfill(conn, embed, args.model, apply=True, all_rows=args.all)
    finally:
        conn.close()
    report["db"] = str(db)
    if backup:
        report["backup"] = str(backup)
    print(json.dumps(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
