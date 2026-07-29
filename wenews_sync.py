#!/usr/bin/env python3
"""wenews (金融我闻) enrichment and daily sync.

Fetches wenews article pages to extract the free preview body, summary, author,
editor, cover image and tags, then stores them in the shared SQLite archive.
Only the freely available preview is captured; paywalled full text is not.

Commands:
    python wenews_sync.py sync                 # discover + fetch new articles
    python wenews_sync.py backfill --limit 50  # enrich already-imported rows
    python wenews_sync.py retag                # rebuild tags for all wenews rows
"""

from __future__ import annotations

import argparse
import html
import re
import sqlite3
import sys
import time
from pathlib import Path

from caixin_archive import ARTICLE_RE, ArchiveDB, Fetcher, clean_url, now_iso
from migrate_excel import ensure_schema
from build_graph import ensure_graph_schema, process_article

WENEWS_HOST = "wenews.caixin.com"
WENEWS_SEEDS = ["https://wenews.caixin.com/"]
ART_URL_RE = re.compile(r"https?://wenews\.caixin\.com/\d{4}-\d{2}-\d{2}/\d+\.html")
META_RE = re.compile(r'<meta[^>]+>', re.I)
BODY_RE = re.compile(r'<div[^>]*id="Main_Content_Val"[^>]*>(.*?)</div>\s*(?:<div|<script|<!--)', re.S | re.I)
INFO_RE = re.compile(r'<div[^>]*id="the_content"[^>]*>(.*?)</div>', re.S | re.I)
TIME_RE = re.compile(r'(20\d{2})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?')
AUTHOR_RE = re.compile(r'作者[：:]\s*([^<]{1,30}?)(?:责任编辑|来源|$)')
EDITOR_RE = re.compile(r'责任编辑[：:]\s*([^\s<，,]{1,20})')

# Keyword -> category tag rules. A title/summary may match several categories.
CATEGORY_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("银行", ("银行", "信贷", "存款", "贷款", "理财", "不良", "揽储", "支行", "分行", "余额")),
    ("保险", ("保险", "险企", "保费", "寿险", "财险", "再保", "偿付", "保司")),
    ("证券基金", ("券商", "证券", "基金", "公募", "私募", "IPO", "股市", "A股", "债券", "上市", "股权", "定增")),
    ("房地产", ("房地产", "楼市", "地产", "房企", "房价", "土地", "物业", "拿地")),
    ("监管政策", ("监管", "证监会", "银保监", "金融监管总局", "处罚", "罚单", "合规", "立案", "整改", "窗口指导")),
    ("宏观经济", ("货币政策", "GDP", "通胀", "利率", "汇率", "财政", "债务", "央行", "降息", "降准", "社融", "货币")),
    ("科技金融", ("金融科技", "支付", "数字货币", "比特币", "加密", "区块链", "互联网金融", "数字人民币", "AI", "人工智能")),
    ("公司人事", ("高管", "人事", "董事长", "总裁", "行长", "任职", "辞职", "落定", "履新", "调整", "干部", "提拔", "晋升")),
    ("案件调查", ("内幕交易", "诈骗", "追索", "判决", "庭审", "被查", "落马", "案", "违规", "举报", "受贿", "行贿", "腐败", "反腐")),
    ("信托", ("信托", "资管", "通道", "融资", "城投", "信政", "非标")),
    ("期货大宗", ("期货", "大宗", "原油", "黄金", "铜", "铁矿石", "煤炭", "天然气", "商品")),
    ("国际金融", ("美联储", "美元", "美国", "欧洲", "香港", "跨境", "海外", "外资", "离岸", "美股", "港股")),
    ("私募股权", ("PE", "VC", "投资", "并购", "退出", "对赌", "LP", "GP", "创业", "融资", "独角兽")),
    ("互联网金融", ("互联网金融", "P2P", "网贷", "众筹", "消费金融", "现金贷", "助贷", "互联网银行")),
]


def strip_tags(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", "", fragment)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def parse_meta(body: str) -> dict[str, str]:
    meta: dict[str, str] = {}
    for tag in META_RE.findall(body):
        key = re.search(r'(?:property|name)="([^"]+)"', tag, re.I)
        val = re.search(r'content="([^"]*)"', tag, re.I)
        if key and val:
            meta[key.group(1).lower()] = html.unescape(val.group(1)).strip()
    return meta


def derive_tags(title: str, summary: str, keywords: str) -> list[str]:
    tags: list[str] = []
    # keyword tags from meta: comma/、 separated, drop the title itself and long phrases
    for raw in re.split(r"[,，、|]", keywords):
        kw = raw.strip()
        if 1 < len(kw) <= 8 and kw != title and kw not in title[:6]:
            if kw not in tags:
                tags.append(kw)
    # rule-based category tags
    haystack = f"{title} {summary}"
    for category, keys in CATEGORY_RULES:
        if any(k in haystack for k in keys) and category not in tags:
            tags.append(category)
    return tags[:8]


def enrich(url: str, body: str) -> dict:
    meta = parse_meta(body)
    title = meta.get("og:title", "")
    summary = meta.get("og:description", "") or meta.get("description", "")
    image = meta.get("og:image", "")
    keywords = meta.get("keywords", "")

    body_match = BODY_RE.search(body)
    body_text = strip_tags(body_match.group(1)) if body_match else ""

    published_at = ""
    author = editor = ""
    info_match = INFO_RE.search(body)
    info_text = strip_tags(info_match.group(1)) if info_match else strip_tags(body[:4000])
    tm = TIME_RE.search(info_text)
    if tm:
        y, mo, d, hh, mm, ss = tm.groups()
        published_at = f"{y}-{mo}-{d}T{hh}:{mm}:{ss or '00'}+08:00"
    a = AUTHOR_RE.search(info_text)
    e = EDITOR_RE.search(info_text)
    author = a.group(1).strip() if a else ""
    editor = e.group(1).strip() if e else ""

    return {
        "title": title,
        "summary": summary,
        "image": image,
        "body_text": body_text,
        "published_at": published_at,
        "author": author,
        "editor": editor,
        "tags": derive_tags(title, summary, keywords),
    }


def set_tags(conn: sqlite3.Connection, url: str, tags: list[str]) -> None:
    conn.execute("DELETE FROM article_tags WHERE article_url=?", (url,))
    for name in tags:
        conn.execute("INSERT OR IGNORE INTO tags(name) VALUES(?)", (name,))
        tag_id = conn.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()[0]
        conn.execute(
            "INSERT OR IGNORE INTO article_tags(article_url, tag_id) VALUES(?,?)",
            (url, tag_id),
        )


def ensure_extra_columns(conn: sqlite3.Connection) -> None:
    """author / editor / image live only in the website layer."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(articles)")}
    for name in ("author", "editor", "image"):
        if name not in existing:
            conn.execute(f"ALTER TABLE articles ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    conn.commit()


def apply_enrichment(conn: sqlite3.Connection, url: str, data: dict, insert: bool) -> None:
    stamp = now_iso()
    if insert:
        match = ARTICLE_RE.match(url)
        conn.execute(
            """INSERT INTO articles
               (article_id, title, published_at, url, discovered_at, checked_at,
                source, summary, body_text, updated_at, author, editor, image)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(url) DO NOTHING""",
            (match.group(2), data["title"], data["published_at"] or match.group(1), url,
             stamp, stamp, "wenews", data["summary"], data["body_text"], stamp,
             data["author"], data["editor"], data["image"]),
        )
    else:
        conn.execute(
            """UPDATE articles SET
                 title=COALESCE(NULLIF(?, ''), title),
                 published_at=COALESCE(NULLIF(?, ''), published_at),
                 summary=?, body_text=?, author=?, editor=?, image=?, updated_at=?
               WHERE url=?""",
            (data["title"], data["published_at"], data["summary"], data["body_text"],
             data["author"], data["editor"], data["image"], stamp, url),
        )
    set_tags(conn, url, data["tags"])


def discover(fetcher: Fetcher, seeds: list[str], retries: int = 3) -> list[str]:
    found: list[str] = []
    for seed in seeds:
        body = None
        for attempt in range(1, retries + 1):
            try:
                body = fetcher.get(seed)
                break
            except Exception as exc:  # noqa: BLE001
                print(f"discover error {seed} (attempt {attempt}/{retries}): {exc}",
                      file=sys.stderr)
                time.sleep(3 * attempt)
        if body is None:
            continue
        for url in dict.fromkeys(ART_URL_RE.findall(body)):
            cleaned = clean_url(url)
            if cleaned not in found:
                found.append(cleaned)
    return found


def sync(args) -> None:
    db = ArchiveDB(Path(args.db))
    conn = db.conn
    ensure_schema(conn)
    ensure_extra_columns(conn)
    ensure_graph_schema(conn)
    fetcher = Fetcher(args.delay, args.jitter, args.timeout)

    urls = discover(fetcher, args.seed or WENEWS_SEEDS)
    known = {r[0] for r in conn.execute("SELECT url FROM articles WHERE source='wenews'")}
    new_urls = [u for u in urls if u not in known]
    print(f"discovered={len(urls)} new={len(new_urls)}")

    added = 0
    for url in new_urls:
        try:
            body = fetcher.get(url)
            data = enrich(url, body)
            if not data["title"]:
                print(f"skip (no title) {url}", file=sys.stderr)
                continue
            apply_enrichment(conn, url, data, insert=True)
            # Extract entities and build co-occurrence relationships
            process_article(conn, url, data["title"], data["summary"],
                            data["published_at"] or "")
            conn.commit()
            added += 1
            print(f"NEW {data['published_at'][:10]} {data['title']}")
        except Exception as exc:  # noqa: BLE001
            print(f"ERR {url}: {exc}", file=sys.stderr)
    print(f"added={added}")


def backfill(args) -> None:
    db = ArchiveDB(Path(args.db))
    conn = db.conn
    ensure_schema(conn)
    ensure_extra_columns(conn)
    fetcher = Fetcher(args.delay, args.jitter, args.timeout)

    rows = conn.execute(
        "SELECT url FROM articles WHERE source='wenews' AND (summary='' OR body_text='') "
        "ORDER BY published_at DESC LIMIT ?",
        (args.limit,),
    ).fetchall()
    print(f"to_enrich={len(rows)}")
    done = 0
    for (url,) in rows:
        try:
            body = fetcher.get(url)
            data = enrich(url, body)
            apply_enrichment(conn, url, data, insert=False)
            conn.commit()
            done += 1
            print(f"OK {url}")
        except Exception as exc:  # noqa: BLE001
            print(f"ERR {url}: {exc}", file=sys.stderr)
    print(f"enriched={done}")


def retag(args) -> None:
    db = ArchiveDB(Path(args.db))
    conn = db.conn
    ensure_schema(conn)
    rows = conn.execute(
        "SELECT url, title, summary FROM articles WHERE source='wenews'"
    ).fetchall()
    for url, title, summary in rows:
        set_tags(conn, url, derive_tags(title or "", summary or "", ""))
    conn.commit()
    total = conn.execute("SELECT count(*) FROM tags").fetchone()[0]
    print(f"retagged={len(rows)} distinct_tags={total}")


def cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="caixin.db")
    p.add_argument("--delay", type=float, default=3.0)
    p.add_argument("--jitter", type=float, default=2.0)
    p.add_argument("--timeout", type=float, default=30)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync"); s.add_argument("--seed", action="append"); s.set_defaults(func=sync)
    b = sub.add_parser("backfill"); b.add_argument("--limit", type=int, default=50); b.set_defaults(func=backfill)
    r = sub.add_parser("retag"); r.set_defaults(func=retag)
    return p


if __name__ == "__main__":
    ns = cli().parse_args()
    ns.func(ns)
