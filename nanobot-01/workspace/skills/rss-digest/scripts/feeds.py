#!/usr/bin/env python3
"""
feeds.py — RSS/Atom feed manager for Sovereign nanobot-01.

Replaces the 'feed' Go CLI for rss-digest and compatible skills.
Feed subscriptions stored in /workspace/feeds/subscriptions.json.
All output is JSON to stdout. Exit code 1 on error.
"""

import argparse
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

try:
    import feedparser
except ImportError:
    print(json.dumps({"status": "error", "error": "feedparser not installed — run pip install feedparser"}))
    sys.exit(1)

try:
    import httpx
except ImportError:
    httpx = None

FEEDS_DIR = Path(os.environ.get("FEEDS_DIR", "/workspace/feeds"))
SUBS_FILE = FEEDS_DIR / "subscriptions.json"


def _load_subs() -> dict:
    FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    if not SUBS_FILE.exists():
        return {"feeds": [], "last_updated": None}
    try:
        return json.loads(SUBS_FILE.read_text())
    except Exception:
        return {"feeds": [], "last_updated": None}


def _save_subs(subs: dict):
    FEEDS_DIR.mkdir(parents=True, exist_ok=True)
    subs["last_updated"] = datetime.now(timezone.utc).isoformat()
    SUBS_FILE.write_text(json.dumps(subs, indent=2))


def _parse_dt(entry) -> str:
    for field in ("published_parsed", "updated_parsed", "created_parsed"):
        t = getattr(entry, field, None)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc).isoformat()
            except Exception:
                pass
    return ""


def _entry_to_dict(entry, feed_title: str) -> dict:
    summary = getattr(entry, "summary", "") or ""
    summary = re.sub(r"<[^>]+>", " ", summary).strip()
    summary = " ".join(summary.split())[:300]
    return {
        "title": getattr(entry, "title", "(no title)"),
        "feed": feed_title,
        "url": getattr(entry, "link", ""),
        "date": _parse_dt(entry),
        "summary": summary,
    }


def cmd_get_entries(args):
    subs = _load_subs()
    limit = args.limit
    category_filter = (args.category or "").lower()
    entries = []
    for feed_info in subs.get("feeds", []):
        if category_filter and feed_info.get("category", "").lower() != category_filter:
            continue
        try:
            parsed = feedparser.parse(feed_info["url"])
            feed_title = parsed.feed.get("title", feed_info.get("name", feed_info["url"]))
            for entry in parsed.entries:
                entries.append(_entry_to_dict(entry, feed_title))
        except Exception:
            pass
    entries.sort(key=lambda x: x["date"], reverse=True)
    entries = entries[:limit]
    print(json.dumps({"status": "ok", "entries": entries, "count": len(entries)}))


def cmd_get_entry(args):
    url = args.url
    if not url:
        print(json.dumps({"status": "error", "error": "url is required"}))
        sys.exit(1)
    if httpx is None:
        print(json.dumps({"status": "error", "error": "httpx not installed"}))
        sys.exit(1)
    try:
        r = httpx.get(url, follow_redirects=True, timeout=15,
                      headers={"User-Agent": "Sovereign/1.0 (RSS reader)"})
        text = re.sub(r"<[^>]+>", " ", r.text)
        text = " ".join(text.split())
        word_count = len(text.split())
        print(json.dumps({"status": "ok", "url": url, "content": text[:4000], "word_count": word_count}))
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e), "url": url}))
        sys.exit(1)


def cmd_add_feed(args):
    if not args.name or not args.url:
        print(json.dumps({"status": "error", "error": "name and url are required"}))
        sys.exit(1)
    subs = _load_subs()
    for f in subs["feeds"]:
        if f["url"] == args.url:
            print(json.dumps({"status": "ok", "action": "already_exists", "name": f["name"], "url": f["url"]}))
            return
    subs["feeds"].append({
        "name": args.name,
        "url": args.url,
        "category": args.category or "general",
        "added": datetime.now(timezone.utc).isoformat(),
    })
    _save_subs(subs)
    print(json.dumps({"status": "ok", "action": "added", "name": args.name, "url": args.url}))


def cmd_list_feeds(args):
    subs = _load_subs()
    feeds = subs.get("feeds", [])
    print(json.dumps({"status": "ok", "feeds": feeds, "count": len(feeds)}))


def cmd_search(args):
    if not args.query:
        print(json.dumps({"status": "error", "error": "query is required"}))
        sys.exit(1)
    query = args.query.lower()
    subs = _load_subs()
    matches = []
    for feed_info in subs.get("feeds", []):
        try:
            parsed = feedparser.parse(feed_info["url"])
            feed_title = parsed.feed.get("title", feed_info.get("name", feed_info["url"]))
            for entry in parsed.entries:
                d = _entry_to_dict(entry, feed_title)
                if query in d["title"].lower() or query in d["summary"].lower():
                    matches.append(d)
        except Exception:
            pass
    matches.sort(key=lambda x: x["date"], reverse=True)
    matches = matches[:args.limit]
    print(json.dumps({"status": "ok", "entries": matches, "count": len(matches), "query": args.query}))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command")

    p = sub.add_parser("get-entries")
    p.add_argument("--limit", type=int, default=20)
    p.add_argument("--category", default="")

    p = sub.add_parser("get-entry")
    p.add_argument("--url", required=True)

    p = sub.add_parser("add-feed")
    p.add_argument("--name", required=True)
    p.add_argument("--url", required=True)
    p.add_argument("--category", default="general")

    sub.add_parser("list-feeds")

    p = sub.add_parser("search")
    p.add_argument("--query", required=True)
    p.add_argument("--limit", type=int, default=10)

    args = parser.parse_args()
    dispatch = {
        "get-entries": cmd_get_entries,
        "get-entry": cmd_get_entry,
        "add-feed": cmd_add_feed,
        "list-feeds": cmd_list_feeds,
        "search": cmd_search,
    }
    fn = dispatch.get(args.command)
    if fn:
        fn(args)
    else:
        print(json.dumps({"status": "error", "error": f"unknown command: {args.command!r}"}))
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"status": "error", "error": str(e), "trace": traceback.format_exc()[-500:]}))
        sys.exit(1)
