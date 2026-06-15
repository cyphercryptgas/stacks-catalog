#!/usr/bin/env python3
"""Build an index of LibriVox audiobooks so the library can mark, filter, and
browse books that have a free recording.

LibriVox's whole catalogue is reachable via its feed API with offset/limit
paging; the catalogue is small (~13k works), so one polite pass captures it.
We store a normalised (title, author-surname) key for fast matching against
shelved books, plus enough metadata to present and open the recording.

Output: audio.db  with
  audiobooks(nt TEXT, sn TEXT, id INTEGER, title TEXT, author TEXT,
             url TEXT, totaltime TEXT, sections INTEGER,
             PRIMARY KEY(nt, sn))
  meta(k TEXT PRIMARY KEY, v TEXT)

Env:
  LIBRIVOX_BASE   default https://librivox.org
  DATA_DIR        where audio.db is written (default ./data)
  LV_LIMIT        page size (default 100)
  LV_PAUSE        seconds between pages (default 4; be polite)
  LV_MAX_OFFSET   safety cap on paging (default 14000)
"""
import json
import os
import sqlite3
import time
import urllib.parse
import urllib.request

LV_BASE = os.environ.get("LIBRIVOX_BASE", "https://librivox.org").rstrip("/")
DATA_DIR = os.environ.get("DATA_DIR", "./data")
LIMIT = int(os.environ.get("LV_LIMIT", "100"))
PAUSE = float(os.environ.get("LV_PAUSE", "4"))
MAX_OFFSET = int(os.environ.get("LV_MAX_OFFSET", "14000"))

UA = "HexadecagonLibrary/1.0 (+https://github.com/cyphercryptgas/stacks-catalog)"
STOP = {"the", "a", "an", "or", "and"}

# Map LibriVox genre strings to the library's 8 broad categories. LibriVox genres
# are hierarchical (e.g. "Children's Fiction > Action & Adventure"); we match on
# keywords so the whole tree collapses into the same eight shelves the books use.
CATEGORY_RULES = [
    ("For Younger Readers", ["children", "juvenile", "young adult"]),
    ("Mind & Society", ["political", "politic", "economic", "econom", "social science",
                        "sociolog", "law", "jurisprud", "business", "education",
                        "society", "war & military", "reference", "essays"]),
    ("Stories & Verse", ["fiction", "poetry", "drama", "fantasy", "fairy",
                          "humor", "satire", "romance", "horror", "adventure",
                          "literature", "short stories", "epic", "tragedy", "saga",
                          "nautical", "western", "crime", "mystery", "detective",
                          "action", "ballad", "verse", "play"]),
    ("Lives & History", ["biography", "memoir", "autobiograph", "history", "historical",
                          "letters", "diary", "antiquity"]),
    ("Thought & Belief", ["religion", "philosophy", "ethic", "christ", "bible",
                          "spiritual", "theolog", "sacred", "buddh", "islam",
                          "myth", "psycholog", "self-help"]),
    ("Science & Nature", ["science", "nature", "natural history", "physic", "chemistr",
                          "biolog", "astronom", "math", "medic", "technolog", "animal",
                          "botan", "geolog", "engineering"]),
    ("Living & Place", ["travel", "cook", "garden", "health", "geograph", "sport",
                        "exploration", "house", "domestic", "craft"]),
    ("Art & Culture", ["art", "music", "architect", "photograph", "paint", "theater",
                       "theatre", "design", "language", "culture"]),
]


def category_for(genres):
    """Pick the best-fitting library category for a list of LibriVox genre strings."""
    blob = " ".join(genres).lower()
    if not blob:
        return ""
    # children's takes priority so juvenile fiction doesn't fall into Stories & Verse
    for cat, keys in CATEGORY_RULES:
        for k in keys:
            if k in blob:
                return cat
    return ""


def norm_title(s):
    s = (s or "").lower()
    out = []
    for ch in s:
        out.append(ch if (ch.isalnum() or ch == " ") else " ")
    words = [w for w in "".join(out).split() if w not in STOP]
    return " ".join(words).strip()


def surname(a):
    w = (a or "").strip().split()
    if not w:
        return ""
    return "".join(c for c in w[-1].lower() if c.isalpha())


def get(url, tries=3):
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:  # noqa: BLE001
            if attempt == tries - 1:
                raise
            time.sleep(3 * (attempt + 1))
    return None


def main():
    os.makedirs(DATA_DIR, exist_ok=True)
    db_path = os.path.join(DATA_DIR, "audio.db")
    con = sqlite3.connect(db_path)
    con.executescript(
        "PRAGMA journal_mode=WAL;"
        "CREATE TABLE IF NOT EXISTS audiobooks("
        " nt TEXT, sn TEXT, id INTEGER, title TEXT, author TEXT,"
        " url TEXT, totaltime TEXT, sections INTEGER, cat TEXT,"
        " PRIMARY KEY(nt, sn));"
        "CREATE INDEX IF NOT EXISTS ix_nt ON audiobooks(nt);"
        "CREATE INDEX IF NOT EXISTS ix_cat ON audiobooks(cat);"
        "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);")

    total = 0
    offset = 0
    while offset <= MAX_OFFSET:
        url = (LV_BASE + "/api/feed/audiobooks/?format=json&extended=1&limit=%d&offset=%d"
               % (LIMIT, offset))
        try:
            j = get(url)
        except Exception as e:  # noqa: BLE001
            print("page offset %d failed: %s (stopping)" % (offset, e), flush=True)
            break
        books = (j or {}).get("books") or []
        if not isinstance(books, list) or not books:
            print("no more books at offset %d" % offset, flush=True)
            break
        rows = []
        for b in books:
            lang = (b.get("language") or "")
            if lang and "english" not in lang.lower():
                continue
            title = (b.get("title") or "").strip()
            if not title:
                continue
            auths = b.get("authors") or []
            author = ""
            if auths:
                a0 = auths[0]
                author = ((a0.get("first_name") or "") + " " + (a0.get("last_name") or "")).strip()
            nt = norm_title(title)
            sn = surname(author)
            if not nt:
                continue
            genres = []
            for g in (b.get("genres") or []):
                name = g.get("name") if isinstance(g, dict) else g
                if name:
                    genres.append(str(name))
            cat = category_for(genres)
            rows.append((nt, sn, b.get("id"), title[:300], author[:200],
                         b.get("url_librivox") or None, b.get("totaltime") or None,
                         int(b.get("num_sections") or 0), cat))
        if rows:
            con.executemany(
                "INSERT OR REPLACE INTO audiobooks VALUES(?,?,?,?,?,?,?,?,?)", rows)
            con.commit()
            total += len(rows)
        print("offset %5d  +%3d english  (total %d)" % (offset, len(rows), total), flush=True)
        offset += LIMIT
        time.sleep(PAUSE)

    con.execute("INSERT OR REPLACE INTO meta VALUES('count', ?)", (str(total),))
    con.execute("INSERT OR REPLACE INTO meta VALUES('built', ?)",
                (time.strftime("%Y%m%d"),))
    con.commit()
    con.close()
    print("done: %d english audiobooks indexed -> %s" % (total, db_path), flush=True)


if __name__ == "__main__":
    main()
