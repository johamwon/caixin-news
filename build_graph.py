#!/usr/bin/env python3
"""Build knowledge graph from extracted entities.

Extracts entities from all wenews articles, stores them in the database,
and creates co-occurrence relationships.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

from entity_extractor import extract_entities

DB_PATH = Path("caixin.db")


def ensure_graph_schema(conn: sqlite3.Connection) -> None:
    """Create tables for entities and relationships."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS entities(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        type TEXT NOT NULL CHECK(type IN ('person', 'org', 'event')),
        first_seen TEXT,
        last_seen TEXT,
        UNIQUE(name, type)
    );
    CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(type);
    CREATE INDEX IF NOT EXISTS idx_entities_name ON entities(name);
    
    CREATE TABLE IF NOT EXISTS article_entities(
        article_url TEXT NOT NULL,
        entity_id INTEGER NOT NULL,
        PRIMARY KEY(article_url, entity_id)
    );
    CREATE INDEX IF NOT EXISTS idx_article_entities_entity ON article_entities(entity_id);
    
    CREATE TABLE IF NOT EXISTS relationships(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_a_id INTEGER NOT NULL,
        entity_b_id INTEGER NOT NULL,
        relation_type TEXT NOT NULL DEFAULT 'co-occurrence',
        article_url TEXT,
        strength INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX IF NOT EXISTS idx_relationships_a ON relationships(entity_a_id);
    CREATE INDEX IF NOT EXISTS idx_relationships_b ON relationships(entity_b_id);
    """)
    conn.commit()


def upsert_entity(conn: sqlite3.Connection, name: str, type: str, published_at: str) -> int:
    """Insert or get entity, update first_seen/last_seen."""
    conn.execute(
        """INSERT INTO entities(name, type, first_seen, last_seen)
           VALUES(?,?,?,?)
           ON CONFLICT(name, type) DO UPDATE SET
               last_seen=CASE WHEN excluded.last_seen > entities.last_seen THEN excluded.last_seen ELSE entities.last_seen END,
               first_seen=CASE WHEN excluded.first_seen < entities.first_seen OR entities.first_seen IS NULL THEN excluded.first_seen ELSE entities.first_seen END""",
        (name, type, published_at, published_at),
    )
    row = conn.execute("SELECT id FROM entities WHERE name=? AND type=?", (name, type)).fetchone()
    return row[0]


def process_article(conn: sqlite3.Connection, url: str, title: str,
                    summary: str, published_at: str) -> tuple[int, int]:
    """Extract entities for one article, link them and add co-occurrence
    relationships. Returns (entities_linked, relationships_added)."""
    entities = extract_entities(title or "", summary or "")
    entity_ids = []

    for entity in entities:
        eid = upsert_entity(conn, entity.name, entity.type, published_at or "")
        entity_ids.append(eid)

        # Link article to entity
        try:
            conn.execute(
                "INSERT OR IGNORE INTO article_entities(article_url, entity_id) VALUES(?,?)",
                (url, eid),
            )
        except sqlite3.IntegrityError:
            pass

    # Create co-occurrence relationships between entities in the same article
    rels_added = 0
    for i, eid_a in enumerate(entity_ids):
        for eid_b in entity_ids[i+1:]:
            # Check if relationship already exists for this article
            existing = conn.execute(
                "SELECT id FROM relationships WHERE entity_a_id=? AND entity_b_id=? AND article_url=?",
                (eid_a, eid_b, url),
            ).fetchone()

            if not existing:
                conn.execute(
                    """INSERT INTO relationships(entity_a_id, entity_b_id, relation_type, article_url, strength)
                       VALUES(?,?,?, ?,1)""",
                    (eid_a, eid_b, "co-occurrence", url),
                )
                rels_added += 1
            else:
                # Increment strength
                conn.execute(
                    "UPDATE relationships SET strength=strength+1 WHERE id=?",
                    (existing[0],),
                )

    return len(entity_ids), rels_added


def build_graph(conn: sqlite3.Connection, limit: int = 0) -> dict:
    """Extract entities from articles and build co-occurrence graph."""
    rows = conn.execute(
        "SELECT url, title, summary, published_at FROM articles WHERE source='wenews' ORDER BY published_at DESC"
    ).fetchall()
    
    if limit > 0:
        rows = rows[:limit]
    
    stats = {"articles": 0, "entities_added": 0, "relationships_added": 0}
    
    for url, title, summary, published_at in rows:
        stats["articles"] += 1
        _, rels = process_article(conn, url, title, summary, published_at)
        stats["relationships_added"] += rels
        conn.commit()
    
    # Count total entities
    total_entities = conn.execute("SELECT count(*) FROM entities").fetchone()[0]
    stats["total_entities"] = total_entities
    
    return stats


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=str(DB_PATH))
    parser.add_argument("--limit", type=int, default=0, help="Limit articles to process (0=all)")
    args = parser.parse_args()
    
    conn = sqlite3.connect(args.db)
    ensure_graph_schema(conn)
    
    print(f"Building knowledge graph...")
    stats = build_graph(conn, args.limit)
    
    print(f"articles_processed={stats['articles']}")
    print(f"total_entities={stats['total_entities']}")
    print(f"relationships_added={stats['relationships_added']}")
    
    conn.close()


if __name__ == "__main__":
    main()
