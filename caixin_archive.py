#!/usr/bin/env python3
"""Archive public Caixin article metadata: title, publication time and URL."""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import random
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree as ET

UA = "CaixinMetadataArchive/1.0 (+personal metadata archive; contact: local-user)"
ARTICLE_RE = re.compile(
    r"^https?://(?:[a-z0-9-]+\.)?caixin\.com/(\d{4}-\d{2}-\d{2})/(\d+)\.html$",
    re.I,
)
DATE_RE = re.compile(r"(20\d{2})年(\d{1,2})月(\d{1,2})日(?:\s+(\d{1,2}):(\d{2}))?")
ISO_RE = re.compile(r"20\d{2}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)?")
DEFAULT_SEEDS = [
    "https://www.caixin.com/sitemap.html",
    "https://www.caixin.com/index_scroll/",
    "https://economy.caixin.com/",
    "https://finance.caixin.com/",
    "https://companies.caixin.com/",
    "https://china.caixin.com/",
    "https://international.caixin.com/",
    "https://opinion.caixin.com/",
    "https://science.caixin.com/",
    "https://weekly.caixin.com/",
]
LIST_HOSTS = {
    "www.caixin.com", "economy.caixin.com", "finance.caixin.com",
    "companies.caixin.com", "china.caixin.com", "international.caixin.com",
    "opinion.caixin.com", "science.caixin.com", "weekly.caixin.com",
    "photos.caixin.com", "video.caixin.com",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_url(url: str) -> str:
    url = urldefrag(url.strip())[0]
    p = urlparse(url)
    return p._replace(query="").geturl() if ARTICLE_RE.match(url) else url


def is_caixin(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "caixin.com" or host.endswith(".caixin.com")


class PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.meta: dict[str, str] = {}
        self._href: str | None = None
        self._anchor: list[str] = []
        self._capture: str | None = None
        self._buf: list[str] = []
        self.h1: list[str] = []
        self.title: list[str] = []
        self.text: list[str] = []
        self.jsonld: list[str] = []
        self._jsonld = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "a" and a.get("href"):
            self._href, self._anchor = a["href"], []
        elif tag == "meta" and a.get("content"):
            key = (a.get("property") or a.get("name") or "").lower()
            if key:
                self.meta[key] = a["content"].strip()
        elif tag in ("h1", "title"):
            self._capture, self._buf = tag, []
        elif tag == "script" and "ld+json" in a.get("type", "").lower():
            self._jsonld, self._buf = True, []

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(self._anchor).strip()))
            self._href, self._anchor = None, []
        elif tag == self._capture:
            value = " ".join(self._buf).strip()
            (self.h1 if tag == "h1" else self.title).append(value)
            self._capture, self._buf = None, []
        elif tag == "script" and self._jsonld:
            self.jsonld.append("".join(self._buf))
            self._jsonld, self._buf = False, []

    def handle_data(self, data: str) -> None:
        value = data.strip()
        if not value:
            return
        self.text.append(value)
        if self._href is not None:
            self._anchor.append(value)
        if self._capture or self._jsonld:
            self._buf.append(value)


@dataclass
class Article:
    article_id: str
    title: str
    published_at: str
    url: str


def jsonld_objects(raw: str) -> Iterable[dict]:
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    if isinstance(obj, dict):
        graph = obj.get("@graph")
        return [obj] + ([x for x in graph if isinstance(x, dict)] if isinstance(graph, list) else [])
    return [x for x in obj if isinstance(x, dict)] if isinstance(obj, list) else []


def normalize_date(value: str, fallback: str) -> str:
    value = html.unescape(value).strip()
    m = DATE_RE.search(value)
    if m:
        y, mo, d, hh, mm = m.groups()
        return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}" + (f"T{int(hh):02d}:{int(mm):02d}:00+08:00" if hh else "")
    m = ISO_RE.search(value)
    if m:
        return m.group(0)
    return fallback


def extract_article(url: str, body: str, link_title: str = "") -> Article | None:
    match = ARTICLE_RE.match(clean_url(url))
    if not match:
        return None
    parser = PageParser()
    parser.feed(body)
    candidates_title: list[str] = []
    candidates_date: list[str] = []
    for raw in parser.jsonld:
        for obj in jsonld_objects(raw):
            candidates_title.extend(str(obj.get(k, "")) for k in ("headline", "name"))
            candidates_date.extend(str(obj.get(k, "")) for k in ("datePublished", "dateCreated"))
    candidates_title += [parser.meta.get("og:title", ""), *parser.h1, link_title, *parser.title]
    candidates_date += [
        parser.meta.get(k, "")
        for k in ("article:published_time", "date", "pubdate", "publishdate")
    ]
    page_text = " ".join(parser.text[:500])
    candidates_date.append(page_text)
    title = next((re.sub(r"\s+", " ", x).strip() for x in candidates_title if x and len(x.strip()) > 2), "")
    title = re.sub(r"_(?:[^_]*_)?财新网$", "", title).strip()
    published = next((normalize_date(x, "") for x in candidates_date if normalize_date(x, "")), match.group(1))
    return Article(match.group(2), title, published, clean_url(url))


class ArchiveDB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS articles(
          article_id TEXT NOT NULL, title TEXT NOT NULL, published_at TEXT NOT NULL,
          url TEXT PRIMARY KEY, discovered_at TEXT NOT NULL, checked_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_articles_date ON articles(published_at DESC);
        CREATE INDEX IF NOT EXISTS idx_articles_id ON articles(article_id);
        CREATE TABLE IF NOT EXISTS frontier(
          url TEXT PRIMARY KEY, kind TEXT NOT NULL, link_title TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
          error TEXT, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS fetch_log(
          id INTEGER PRIMARY KEY AUTOINCREMENT, requested_at TEXT NOT NULL,
          url TEXT NOT NULL, host TEXT NOT NULL, outcome TEXT NOT NULL,
          http_status INTEGER, elapsed_ms INTEGER NOT NULL, detail TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_fetch_log_time ON fetch_log(requested_at DESC);
        CREATE TABLE IF NOT EXISTS host_state(
          host TEXT PRIMARY KEY, paused_until TEXT, reason TEXT,
          consecutive_errors INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL
        );
        """)
        try:
            self.conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(title, url UNINDEXED, content='articles', content_rowid='rowid')")
            self.conn.executescript("""
            CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
              INSERT INTO articles_fts(rowid,title,url) VALUES(new.rowid,new.title,new.url); END;
            CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles BEGIN
              INSERT INTO articles_fts(articles_fts,rowid,title,url) VALUES('delete',old.rowid,old.title,old.url);
              INSERT INTO articles_fts(rowid,title,url) VALUES(new.rowid,new.title,new.url); END;
            """)
        except sqlite3.OperationalError:
            pass
        self.conn.commit()

    def enqueue(self, url: str, kind: str, title: str = "") -> None:
        self.conn.execute("INSERT OR IGNORE INTO frontier(url,kind,link_title,updated_at) VALUES(?,?,?,?)", (url, kind, title, now_iso()))

    def next(self):
        now = now_iso()
        self.conn.execute("""UPDATE frontier SET status='pending'
          WHERE status='rate_limited' AND updated_at <= ?""", (now,))
        rows = self.conn.execute("""SELECT url,kind,link_title,attempts FROM frontier
          WHERE status='pending' ORDER BY CASE kind WHEN 'article' THEN 0 ELSE 1 END,rowid""")
        for row in rows:
            host = (urlparse(row[0]).hostname or "").lower()
            state = self.conn.execute("SELECT paused_until FROM host_state WHERE host=?", (host,)).fetchone()
            if not state or not state[0] or state[0] <= now:
                return row
        return None

    def done(self, url: str, error: str | None = None) -> None:
        self.conn.execute("UPDATE frontier SET status=?, attempts=attempts+1, error=?, updated_at=? WHERE url=?", ("error" if error else "done", error, now_iso(), url))

    def save(self, a: Article) -> None:
        stamp = now_iso()
        self.conn.execute("""INSERT INTO articles VALUES(?,?,?,?,?,?)
          ON CONFLICT(url) DO UPDATE SET title=excluded.title,published_at=excluded.published_at,checked_at=excluded.checked_at""",
          (a.article_id, a.title, a.published_at, a.url, stamp, stamp))

    def log_fetch(self, url: str, outcome: str, status: int | None, elapsed_ms: int, detail: str = "") -> None:
        host = (urlparse(url).hostname or "").lower()
        self.conn.execute("INSERT INTO fetch_log(requested_at,url,host,outcome,http_status,elapsed_ms,detail) VALUES(?,?,?,?,?,?,?)",
                          (now_iso(), url, host, outcome, status, elapsed_ms, detail[:500]))

    def rate_limited(self, url: str, seconds: int, reason: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        until = (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat(timespec="seconds")
        self.conn.execute("UPDATE frontier SET status='rate_limited',attempts=attempts+1,error=?,updated_at=? WHERE url=?",
                          (reason, until, url))
        self.conn.execute("""INSERT INTO host_state(host,paused_until,reason,consecutive_errors,updated_at)
          VALUES(?,?,?,?,?) ON CONFLICT(host) DO UPDATE SET paused_until=excluded.paused_until,
          reason=excluded.reason,consecutive_errors=host_state.consecutive_errors+1,updated_at=excluded.updated_at""",
          (host, until, reason, 1, now_iso()))

    def host_ok(self, url: str) -> None:
        host = (urlparse(url).hostname or "").lower()
        self.conn.execute("""INSERT INTO host_state(host,paused_until,reason,consecutive_errors,updated_at)
          VALUES(?,NULL,NULL,0,?) ON CONFLICT(host) DO UPDATE SET paused_until=NULL,reason=NULL,
          consecutive_errors=0,updated_at=excluded.updated_at""", (host, now_iso()))


class Fetcher:
    def __init__(self, delay: float, jitter: float, timeout: float):
        self.delay, self.jitter, self.timeout = delay, jitter, timeout
        self.last_by_host: dict[str, float] = {}
        self.robots: dict[str, RobotFileParser] = {}

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        root = f"{p.scheme}://{p.netloc}"
        if root not in self.robots:
            rp = RobotFileParser(root + "/robots.txt")
            try:
                rp.read()
            except Exception:
                return False
            self.robots[root] = rp
        return self.robots[root].can_fetch(UA, url)

    def get(self, url: str) -> str:
        if not self.allowed(url):
            raise PermissionError("robots.txt does not allow this URL")
        host = (urlparse(url).hostname or "").lower()
        target_delay = self.delay + random.uniform(0, max(0, self.jitter))
        wait = target_delay - (time.monotonic() - self.last_by_host.get(host, 0.0))
        if wait > 0:
            time.sleep(wait)
        req = Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml"})
        with urlopen(req, timeout=self.timeout) as res:
            charset = res.headers.get_content_charset() or "utf-8"
            data = res.read(5_000_000)
        self.last_by_host[host] = time.monotonic()
        return data.decode(charset, "replace")


def should_follow(url: str) -> bool:
    p = urlparse(url)
    path = p.path.lower()
    if ARTICLE_RE.match(url):
        return True
    if not is_caixin(url) or (p.hostname or "").lower() not in LIST_HOSTS or p.query:
        return False
    return any(x in path for x in ("index", "sitemap", "archive", "list", "scroll", "page")) or path in ("", "/")


def crawl(args) -> None:
    db, fetcher = ArchiveDB(Path(args.db)), Fetcher(args.delay, args.jitter, args.timeout)
    seeds = args.seed or DEFAULT_SEEDS
    for seed in seeds:
        db.enqueue(clean_url(seed), "list")
    processed = 0
    while (row := db.next()) and (args.max_pages == 0 or processed < args.max_pages):
        url, kind, link_title, attempts = row
        started = time.monotonic()
        try:
            body = fetcher.get(url)
            if ARTICLE_RE.match(url):
                article = extract_article(url, body, link_title)
                if article and article.title:
                    db.save(article)
            else:
                parser = PageParser(); parser.feed(body)
                for href, text in parser.links:
                    target = clean_url(urljoin(url, href))
                    if should_follow(target):
                        db.enqueue(target, "article" if ARTICLE_RE.match(target) else "list", text)
            db.done(url)
            db.host_ok(url)
            db.log_fetch(url, "ok", 200, int((time.monotonic() - started) * 1000))
            print(f"OK  {url}")
        except HTTPError as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            db.log_fetch(url, "http_error", exc.code, elapsed, str(exc))
            if exc.code in (403, 429, 503):
                retry = exc.headers.get("Retry-After") if exc.headers else None
                try:
                    cooldown = int(retry) if retry else args.cooldown
                except ValueError:
                    try:
                        cooldown = max(60, int((parsedate_to_datetime(retry) - datetime.now(timezone.utc)).total_seconds()))
                    except Exception:
                        cooldown = args.cooldown
                db.rate_limited(url, cooldown, f"HTTP {exc.code}")
                print(f"LIMIT {url}: HTTP {exc.code}; host paused {cooldown}s", file=sys.stderr)
            else:
                db.done(url, f"HTTPError: {exc}")
                print(f"ERR {url}: {exc}", file=sys.stderr)
        except (HTTPError, URLError, TimeoutError, PermissionError, OSError) as exc:
            db.log_fetch(url, "error", None, int((time.monotonic() - started) * 1000), str(exc))
            db.done(url, f"{type(exc).__name__}: {exc}")
            print(f"ERR {url}: {exc}", file=sys.stderr)
        db.conn.commit(); processed += 1
    count = db.conn.execute("SELECT count(*) FROM articles").fetchone()[0]
    pending = db.conn.execute("SELECT count(*) FROM frontier WHERE status='pending'").fetchone()[0]
    print(f"articles={count} processed={processed} pending={pending}")


def import_urls(args) -> None:
    db = ArchiveDB(Path(args.db))
    added = 0
    for line in Path(args.file).read_text(encoding="utf-8-sig").splitlines():
        url = clean_url(line.strip())
        if ARTICLE_RE.match(url):
            before = db.conn.total_changes; db.enqueue(url, "article")
            added += db.conn.total_changes > before
    db.conn.commit(); print(f"queued={added}")


def search(args) -> None:
    db = ArchiveDB(Path(args.db))
    q = f"%{args.query}%"
    rows = db.conn.execute("SELECT published_at,title,url FROM articles WHERE title LIKE ? ORDER BY published_at DESC LIMIT ?", (q, args.limit))
    for date, title, url in rows:
        print(f"{date}\t{title}\t{url}")


def export_data(args) -> None:
    db = ArchiveDB(Path(args.db)); out = Path(args.output)
    rows = db.conn.execute("SELECT title,published_at,url FROM articles ORDER BY published_at DESC")
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f); w.writerow(["title", "published_at", "url"]); w.writerows(rows)
    print(f"exported={out}")


def export_rss(args) -> None:
    db = ArchiveDB(Path(args.db)); out = Path(args.output)
    rss = ET.Element("rss", {"version": "2.0"})
    channel = ET.SubElement(rss, "channel")
    ET.SubElement(channel, "title").text = args.title
    ET.SubElement(channel, "link").text = "https://www.caixin.com/"
    ET.SubElement(channel, "description").text = "Local archive of public Caixin article metadata"
    ET.SubElement(channel, "lastBuildDate").text = format_datetime(datetime.now(timezone.utc))
    rows = db.conn.execute("SELECT title,published_at,url FROM articles ORDER BY published_at DESC LIMIT ?", (args.limit,))
    for title, published, url in rows:
        item = ET.SubElement(channel, "item")
        ET.SubElement(item, "title").text = title
        ET.SubElement(item, "link").text = url
        ET.SubElement(item, "guid", {"isPermaLink": "true"}).text = url
        try:
            dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
            ET.SubElement(item, "pubDate").text = format_datetime(dt)
        except ValueError:
            ET.SubElement(item, "pubDate").text = published
    ET.indent(rss)
    ET.ElementTree(rss).write(out, encoding="utf-8", xml_declaration=True)
    db.conn.close()
    print(f"rss_exported={out}")


def stats(args) -> None:
    db = ArchiveDB(Path(args.db))
    for status, n in db.conn.execute("SELECT status,count(*) FROM frontier GROUP BY status"):
        print(f"frontier_{status}={n}")
    row = db.conn.execute("SELECT count(*),min(published_at),max(published_at) FROM articles").fetchone()
    print(f"articles={row[0]} earliest={row[1]} latest={row[2]}")
    for host, until, reason in db.conn.execute("SELECT host,paused_until,reason FROM host_state WHERE paused_until IS NOT NULL ORDER BY host"):
        if until > now_iso():
            print(f"paused_host={host} until={until} reason={reason}")
    fetched = db.conn.execute("SELECT count(*),sum(outcome='ok'),sum(outcome!='ok') FROM fetch_log").fetchone()
    print(f"fetches={fetched[0]} ok={fetched[1] or 0} failed={fetched[2] or 0}")


def cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default="caixin.db")
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("crawl"); c.add_argument("--seed", action="append"); c.add_argument("--delay", type=float, default=3.0); c.add_argument("--jitter", type=float, default=2.0); c.add_argument("--cooldown", type=int, default=1800); c.add_argument("--timeout", type=float, default=20); c.add_argument("--max-pages", type=int, default=100); c.set_defaults(func=crawl)
    i = sub.add_parser("import-urls"); i.add_argument("file"); i.set_defaults(func=import_urls)
    s = sub.add_parser("search"); s.add_argument("query"); s.add_argument("--limit", type=int, default=20); s.set_defaults(func=search)
    e = sub.add_parser("export"); e.add_argument("output", nargs="?", default="caixin_articles.csv"); e.set_defaults(func=export_data)
    r = sub.add_parser("rss"); r.add_argument("output", nargs="?", default="caixin.xml"); r.add_argument("--limit", type=int, default=500); r.add_argument("--title", default="财新文章元数据存档"); r.set_defaults(func=export_rss)
    t = sub.add_parser("stats"); t.set_defaults(func=stats)
    return p


if __name__ == "__main__":
    parser = cli(); ns = parser.parse_args(); ns.func(ns)
