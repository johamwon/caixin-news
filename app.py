#!/usr/bin/env python3
"""Flask website for browsing the Caixin 金融我闻 (wenews) archive.

Vercel deployment copy: identical to the main project's app.py except the
SQLite database is opened read-only (required on Vercel's immutable
filesystem). Served through ``api/index.py`` as a serverless function.

Local run:
    python app.py            # http://0.0.0.0:5000
"""

from __future__ import annotations

import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

from flask import Flask, Response, abort, g, render_template, request

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("CAIXIN_DB", BASE_DIR / "caixin.db"))
SOURCE = "wenews"
PER_PAGE = 24

# Fixed top-level categories (must match wenews_sync.CATEGORY_RULES order).
CATEGORIES = [
    "银行", "保险", "证券基金", "房地产", "监管政策",
    "宏观经济", "科技金融", "公司人事", "案件调查",
    "信托", "期货大宗", "国际金融", "私募股权", "互联网金融",
]

app = Flask(__name__)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        # Read-only connection: Vercel's serverless filesystem is immutable,
        # and the web app never writes. ``immutable=1`` additionally tells
        # SQLite the file cannot change, so it never looks for -wal/-shm
        # sidecar files (which would fail on a WAL-mode db deployed alone).
        conn = sqlite3.connect(
            f"file:{DB_PATH.as_posix()}?mode=ro&immutable=1", uri=True
        )
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def article_tags(db: sqlite3.Connection, url: str) -> list[str]:
    return [
        r["name"]
        for r in db.execute(
            "SELECT t.name FROM tags t JOIN article_tags at ON at.tag_id=t.id "
            "WHERE at.article_url=? ORDER BY t.name",
            (url,),
        )
    ]


def query_articles(db, tag=None, q=None, page=1):
    where = ["a.source = ?"]
    params: list = [SOURCE]
    joins = ""
    if tag:
        joins = ("JOIN article_tags at ON at.article_url = a.url "
                 "JOIN tags t ON t.id = at.tag_id")
        where.append("t.name = ?")
        params.append(tag)
    if q:
        where.append("(a.title LIKE ? OR a.summary LIKE ?)")
        params += [f"%{q}%", f"%{q}%"]
    clause = " AND ".join(where)

    total = db.execute(
        f"SELECT count(DISTINCT a.url) FROM articles a {joins} WHERE {clause}",
        params,
    ).fetchone()[0]

    offset = (page - 1) * PER_PAGE
    rows = db.execute(
        f"""SELECT DISTINCT a.url, a.article_id, a.title, a.published_at,
                   a.summary, a.image
            FROM articles a {joins} WHERE {clause}
            ORDER BY a.published_at DESC LIMIT ? OFFSET ?""",
        params + [PER_PAGE, offset],
    ).fetchall()

    items = []
    for r in rows:
        d = dict(r)
        d["tags"] = article_tags(db, r["url"])
        d["date"] = (r["published_at"] or "")[:10]
        items.append(d)
    return items, total


@app.route("/")
def index():
    db = get_db()
    tag = request.args.get("tag") or None
    q = (request.args.get("q") or "").strip() or None
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1

    items, total = query_articles(db, tag=tag, q=q, page=page)
    pages = max(1, math.ceil(total / PER_PAGE))

    cat_counts = {
        row["name"]: row["n"]
        for row in db.execute(
            """SELECT t.name, count(*) n FROM tags t
               JOIN article_tags at ON at.tag_id = t.id
               JOIN articles a ON a.url = at.article_url AND a.source = ?
               GROUP BY t.name""",
            (SOURCE,),
        )
    }
    grand_total = db.execute(
        "SELECT count(*) FROM articles WHERE source=?", (SOURCE,)
    ).fetchone()[0]

    return render_template(
        "index.html",
        items=items, total=total, page=page, pages=pages,
        tag=tag, q=q, categories=CATEGORIES, cat_counts=cat_counts,
        grand_total=grand_total,
    )


@app.route("/rss")
@app.route("/feed.xml")
def rss():
    """RSS 2.0 feed of the latest articles; ?tag=银行 narrows to one category."""
    db = get_db()
    tag = request.args.get("tag") or None

    joins, where, params = "", ["a.source = ?"], [SOURCE]
    if tag:
        joins = ("JOIN article_tags at ON at.article_url = a.url "
                 "JOIN tags t ON t.id = at.tag_id")
        where.append("t.name = ?")
        params.append(tag)

    rows = db.execute(
        f"""SELECT DISTINCT a.url, a.article_id, a.title, a.published_at,
                   a.summary
            FROM articles a {joins} WHERE {" AND ".join(where)}
            ORDER BY a.published_at DESC LIMIT 50""",
        params,
    ).fetchall()

    site = request.url_root.rstrip("/")
    feed_title = "财新·金融我闻" + (f" · {tag}" if tag else "")
    now_rfc822 = format_datetime(datetime.now(timezone.utc))

    def rfc822(published_at: str | None) -> str:
        # published_at is stored as ISO "YYYY-MM-DD[THH:MM:SS...]" local time.
        raw = (published_at or "")[:19]
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.strptime(raw[: len(datetime.now().strftime(fmt))], fmt)
                return format_datetime(dt.replace(tzinfo=timezone(timedelta(hours=8))))
            except ValueError:
                continue
        return now_rfc822

    items_xml = []
    for r in rows:
        link = f"{site}/a/{r['article_id']}"
        tags = article_tags(db, r["url"])
        cats = "".join(f"<category>{escape(t)}</category>" for t in tags)
        items_xml.append(
            "<item>"
            f"<title>{escape(r['title'] or '')}</title>"
            f"<link>{escape(link)}</link>"
            f"<guid isPermaLink=\"true\">{escape(link)}</guid>"
            f"<pubDate>{rfc822(r['published_at'])}</pubDate>"
            f"<description>{escape(r['summary'] or '')}</description>"
            f"{cats}"
            "</item>"
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">'
        "<channel>"
        f"<title>{escape(feed_title)}</title>"
        f"<link>{escape(site + '/')}</link>"
        "<description>财新「金融我闻」最新文章归档，每日自动更新</description>"
        "<language>zh-cn</language>"
        f"<lastBuildDate>{now_rfc822}</lastBuildDate>"
        f'<atom:link href="{escape(request.url)}" rel="self" '
        'type="application/rss+xml"/>'
        + "".join(items_xml)
        + "</channel></rss>"
    )
    return Response(xml, mimetype="application/rss+xml")


@app.route("/a/<article_id>")
def article(article_id: str):
    db = get_db()
    row = db.execute(
        "SELECT * FROM articles WHERE article_id=? AND source=? LIMIT 1",
        (article_id, SOURCE),
    ).fetchone()
    if row is None:
        abort(404)
    data = dict(row)
    data["tags"] = article_tags(db, row["url"])
    data["date"] = (row["published_at"] or "")[:10]

    # Fetch entities linked to this article (with ids so chips can link out)
    entities = {
        "person": [],
        "org": [],
        "event": [],
    }
    try:
        for r in db.execute(
            """SELECT e.id, e.name, e.type FROM entities e
               JOIN article_entities ae ON ae.entity_id = e.id
               WHERE ae.article_url = ?
               ORDER BY e.type, e.name""",
            (row["url"],),
        ):
            entities[r["type"]].append({"id": r["id"], "name": r["name"]})
    except Exception:
        pass  # tables may not exist yet
    data["entities"] = entities

    # Related articles: prefer shared entities (weighted), fall back to tag
    related = []
    try:
        related = db.execute(
            """SELECT a.article_id, a.title, a.published_at,
                      count(*) AS shared,
                      group_concat(DISTINCT e.name) AS via
               FROM article_entities ae1
               JOIN article_entities ae2
                    ON ae2.entity_id = ae1.entity_id
                   AND ae2.article_url <> ae1.article_url
               JOIN entities e ON e.id = ae1.entity_id
               JOIN articles a ON a.url = ae2.article_url AND a.source = ?
               WHERE ae1.article_url = ?
               GROUP BY a.url
               ORDER BY shared DESC, a.published_at DESC LIMIT 8""",
            (SOURCE, row["url"]),
        ).fetchall()
    except Exception:
        related = []
    if not related and data["tags"]:
        related = db.execute(
            """SELECT DISTINCT a.article_id, a.title, a.published_at,
                      0 AS shared, NULL AS via
               FROM articles a JOIN article_tags at ON at.article_url = a.url
               JOIN tags t ON t.id = at.tag_id
               WHERE t.name = ? AND a.source = ? AND a.article_id <> ?
               ORDER BY a.published_at DESC LIMIT 6""",
            (data["tags"][0], SOURCE, article_id),
        ).fetchall()
    return render_template("article.html", a=data, related=related)


@app.template_filter("nl2p")
def nl2p(text: str) -> str:
    parts = [p.strip() for p in (text or "").split("\n") if p.strip()]
    return "".join(f"<p>{p}</p>" for p in parts)


@app.route("/graph")
def graph():
    """Knowledge graph visualization page."""
    db = get_db()
    
    # Get entity type counts
    type_counts = {
        row["type"]: row["n"]
        for row in db.execute(
            "SELECT type, count(*) n FROM entities GROUP BY type"
        )
    }
    
    # Get top entities by relationship count
    top_entities = db.execute(
        """SELECT e.id, e.name, e.type, count(r.id) as rel_count
           FROM entities e
           LEFT JOIN relationships r ON (r.entity_a_id = e.id OR r.entity_b_id = e.id)
           GROUP BY e.id
           ORDER BY rel_count DESC
           LIMIT 100"""
    ).fetchall()
    
    return render_template(
        "graph.html",
        type_counts=type_counts,
        top_entities=top_entities,
    )


@app.route("/api/graph/data")
def graph_data():
    """API endpoint for graph visualization data.

    Modes:
      - default: top entities by article count (optionally filtered)
      - focus=<entity_id>: ego network of one entity and its neighbours
    """
    db = get_db()

    types = [t for t in request.args.getlist("type")
             if t in ("person", "org", "event")]
    search = request.args.get("q", "").strip()
    limit = min(int(request.args.get("limit", 200)), 500)
    focus = request.args.get("focus")

    if focus:
        # Ego network: the focus entity + all entities linked to it
        entities = db.execute(
            """SELECT e.id, e.name, e.type,
                      count(DISTINCT ae.article_url) AS article_count
               FROM entities e
               LEFT JOIN article_entities ae ON ae.entity_id = e.id
               WHERE e.id = ? OR e.id IN (
                     SELECT CASE WHEN r.entity_a_id = ? THEN r.entity_b_id
                                 ELSE r.entity_a_id END
                     FROM relationships r
                     WHERE r.entity_a_id = ? OR r.entity_b_id = ?)
               GROUP BY e.id
               ORDER BY article_count DESC LIMIT ?""",
            (focus, focus, focus, focus, limit),
        ).fetchall()
    else:
        entity_where = []
        entity_params: list = []

        if types and len(types) < 3:
            placeholders = ",".join("?" * len(types))
            entity_where.append(f"e.type IN ({placeholders})")
            entity_params += types

        if search:
            entity_where.append("e.name LIKE ?")
            entity_params.append(f"%{search}%")

        entity_clause = " AND ".join(entity_where) if entity_where else "1=1"

        entities = db.execute(
            f"""SELECT e.id, e.name, e.type,
                       count(DISTINCT ae.article_url) as article_count
                FROM entities e
                LEFT JOIN article_entities ae ON ae.entity_id = e.id
                WHERE {entity_clause}
                GROUP BY e.id
                ORDER BY article_count DESC
                LIMIT ?""",
            entity_params + [limit],
        ).fetchall()
    
    entity_ids = [e["id"] for e in entities]
    
    # Get relationships between these entities
    relationships = []
    if entity_ids:
        placeholders = ",".join("?" * len(entity_ids))
        relationships = db.execute(
            f"""SELECT r.entity_a_id, r.entity_b_id, r.relation_type, r.strength,
                       ea.name as entity_a_name, ea.type as entity_a_type,
                       eb.name as entity_b_name, eb.type as entity_b_type
                FROM relationships r
                JOIN entities ea ON ea.id = r.entity_a_id
                JOIN entities eb ON eb.id = r.entity_b_id
                WHERE r.entity_a_id IN ({placeholders})
                  AND r.entity_b_id IN ({placeholders})""",
            entity_ids + entity_ids,
        ).fetchall()
    
    # Format for vis-network
    nodes = []
    for e in entities:
        nodes.append({
            "id": e["id"],
            "label": e["name"],
            "group": e["type"],
            "value": e["article_count"],
            "focus": bool(focus) and e["id"] == int(focus),
        })
    
    edges = []
    for r in relationships:
        edges.append({
            "from": r["entity_a_id"],
            "to": r["entity_b_id"],
            "label": r["relation_type"] if r["strength"] > 2 else "",
            "value": r["strength"],
        })
    
    return {"nodes": nodes, "edges": edges, "total": len(nodes)}


@app.route("/entities")
def entities_browse():
    """Browse all entities grouped by type, sorted by article count."""
    db = get_db()
    etype = request.args.get("type", "person")
    if etype not in ("person", "org", "event"):
        etype = "person"
    q = (request.args.get("q") or "").strip()

    where = ["e.type = ?"]
    params: list = [etype]
    if q:
        where.append("e.name LIKE ?")
        params.append(f"%{q}%")
    clause = " AND ".join(where)

    rows = db.execute(
        f"""SELECT e.id, e.name, e.type,
                   count(DISTINCT ae.article_url) AS article_count,
                   max(a.published_at) AS latest
            FROM entities e
            LEFT JOIN article_entities ae ON ae.entity_id = e.id
            LEFT JOIN articles a ON a.url = ae.article_url
            WHERE {clause}
            GROUP BY e.id
            HAVING article_count > 0
            ORDER BY article_count DESC, latest DESC""",
        params,
    ).fetchall()

    type_counts = {
        row["type"]: row["n"]
        for row in db.execute(
            """SELECT e.type, count(DISTINCT e.id) n FROM entities e
               JOIN article_entities ae ON ae.entity_id = e.id
               GROUP BY e.type"""
        )
    }

    return render_template(
        "entities.html",
        etype=etype, q=q, rows=rows, type_counts=type_counts,
    )


@app.route("/entity/<int:entity_id>")
def entity_detail(entity_id: int):
    """Entity page: all its articles plus co-occurring entities."""
    db = get_db()
    ent = db.execute(
        "SELECT id, name, type FROM entities WHERE id = ?", (entity_id,)
    ).fetchone()
    if ent is None:
        abort(404)

    articles = db.execute(
        """SELECT a.article_id, a.title, a.summary, a.published_at
           FROM articles a
           JOIN article_entities ae ON ae.article_url = a.url
           WHERE ae.entity_id = ? AND a.source = ?
           ORDER BY a.published_at DESC""",
        (entity_id, SOURCE),
    ).fetchall()

    # Co-occurring entities with shared-article counts (via relationships)
    related = db.execute(
        """SELECT e.id, e.name, e.type, r.strength
           FROM relationships r
           JOIN entities e ON e.id = CASE
                WHEN r.entity_a_id = ? THEN r.entity_b_id
                ELSE r.entity_a_id END
           WHERE r.entity_a_id = ? OR r.entity_b_id = ?
           ORDER BY r.strength DESC, e.name LIMIT 30""",
        (entity_id, entity_id, entity_id),
    ).fetchall()

    return render_template(
        "entity.html", ent=ent, articles=articles, related=related,
    )


@app.route("/api/entity/<int:entity_id>")
def api_entity(entity_id: int):
    """JSON: entity summary for the graph side panel."""
    db = get_db()
    ent = db.execute(
        "SELECT id, name, type FROM entities WHERE id = ?", (entity_id,)
    ).fetchone()
    if ent is None:
        abort(404)

    articles = db.execute(
        """SELECT a.article_id, a.title, a.published_at
           FROM articles a
           JOIN article_entities ae ON ae.article_url = a.url
           WHERE ae.entity_id = ? AND a.source = ?
           ORDER BY a.published_at DESC LIMIT 10""",
        (entity_id, SOURCE),
    ).fetchall()

    related = db.execute(
        """SELECT e.id, e.name, e.type, r.strength
           FROM relationships r
           JOIN entities e ON e.id = CASE
                WHEN r.entity_a_id = ? THEN r.entity_b_id
                ELSE r.entity_a_id END
           WHERE r.entity_a_id = ? OR r.entity_b_id = ?
           ORDER BY r.strength DESC LIMIT 12""",
        (entity_id, entity_id, entity_id),
    ).fetchall()

    total = db.execute(
        "SELECT count(*) FROM article_entities WHERE entity_id = ?",
        (entity_id,),
    ).fetchone()[0]

    return {
        "id": ent["id"], "name": ent["name"], "type": ent["type"],
        "article_count": total,
        "articles": [dict(a) for a in articles],
        "related": [dict(r) for r in related],
    }


if __name__ == "__main__":
    # Debug/reloader off by default: the watchdog reloader misfires on this
    # machine (site-packages false positives) and kills in-flight requests.
    # Enable with FLASK_DEBUG=1 when needed.
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=debug)
