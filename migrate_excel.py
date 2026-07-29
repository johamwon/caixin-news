#!/usr/bin/env python3
"""Import historical wenews (金融我闻) articles from Excel into the SQLite archive.

Reads an Excel file with two columns (标题, URL), parses the publication date and
article id from the URL, and upserts rows into the shared ``articles`` table with
``source='wenews'``. Existing rows are never duplicated (URL is the primary key).

Usage:
    python migrate_excel.py --xlsx caixin_wenews_complete.xlsx --db caixin.db
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

import openpyxl

from caixin_archive import ARTICLE_RE, ArchiveDB, clean_url, now_iso


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Extend the base schema with website-oriented columns and tag tables."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
    additions = {
        "source": "TEXT NOT NULL DEFAULT ''",
        "summary": "TEXT NOT NULL DEFAULT ''",
        "body_text": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
    }
    for name, ddl in additions.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {name} {ddl}")
    conn.executescript("""
    CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source);
    CREATE TABLE IF NOT EXISTS tags(
      id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL
    );
    CREATE TABLE IF NOT EXISTS article_tags(
      article_url TEXT NOT NULL, tag_id INTEGER NOT NULL,
      PRIMARY KEY(article_url, tag_id)
    );
    CREATE INDEX IF NOT EXISTS idx_article_tags_tag ON article_tags(tag_id);
    """)
    conn.commit()


def import_excel(xlsx: Path, db_path: Path, source: str = "wenews") -> dict[str, int]:
    ArchiveDB(db_path).conn.close()  # ensure base schema/tables exist
    conn = sqlite3.connect(db_path)
    ensure_schema(conn)

    wb = openpyxl.load_workbook(xlsx, read_only=True)
    ws = wb.active
    stamp = now_iso()
    stats = {"rows": 0, "inserted": 0, "skipped_bad_url": 0, "already_present": 0}

    for title, url in ws.iter_rows(min_row=2, values_only=True):
        if not url:
            continue
        stats["rows"] += 1
        cleaned = clean_url(str(url).strip())
        match = ARTICLE_RE.match(cleaned)
        if not match:
            stats["skipped_bad_url"] += 1
            continue
        published_at = match.group(1)      # YYYY-MM-DD from the URL
        article_id = match.group(2)        # numeric id from the URL
        clean_title = (str(title).strip() if title else "").replace("\u3000", " ")

        before = conn.total_changes
        conn.execute(
            """INSERT INTO articles
               (article_id, title, published_at, url, discovered_at, checked_at,
                source, summary, body_text, updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(url) DO NOTHING""",
            (article_id, clean_title, published_at, cleaned, stamp, stamp,
             source, "", "", stamp),
        )
        if conn.total_changes > before:
            stats["inserted"] += 1
        else:
            stats["already_present"] += 1

    conn.commit()
    conn.close()
    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--xlsx", default="caixin_wenews_complete.xlsx")
    parser.add_argument("--db", default="caixin.db")
    parser.add_argument("--source", default="wenews")
    args = parser.parse_args()

    stats = import_excel(Path(args.xlsx), Path(args.db), args.source)
    print(
        f"rows={stats['rows']} inserted={stats['inserted']} "
        f"already_present={stats['already_present']} "
        f"skipped_bad_url={stats['skipped_bad_url']}"
    )


if __name__ == "__main__":
    main()
