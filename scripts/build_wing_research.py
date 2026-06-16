#!/usr/bin/env python3
"""Build the Research Wing index: the most-cited works per OpenAlex field.

Each of OpenAlex's ~26 top-level fields becomes one hall of the wing's ring;
within a hall, works shelve by citation count - the famous by the door,
the obscure down the stair. Citation count is the wing's edition count.

Env:
  OPENALEX_BASE    default https://api.openalex.org
  OPENALEX_MAILTO  your email (joins OpenAlex's polite pool - faster, kinder)
  PER_FIELD        works per field (default 10240 = 16 rooms of 640)
  OUT              output sqlite path (default research.db)
"""
import datetime
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

BASE = os.environ.get("OPENALEX_BASE", "https://api.openalex.org").rstrip("/")
MAILTO = os.environ.get("OPENALEX_MAILTO", "")
PER_FIELD = int(os.environ.get("PER_FIELD", "10240"))
OUT = os.environ.get("OUT", "research.db")
SELECT = "id,display_name,authorships,publication_year,cited_by_count,best_oa_location,doi"


def get(url, tries=5):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "hexadecagon-library-wing-builder"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:  # surface the API's complaint
            body = ""
            try:
                body = e.read().decode("utf-8")[:300]
            except Exception:  # noqa: BLE001
                pass
            last = "HTTP %s %s" % (e.code, body)
            if e.code in (400, 403, 404):  # a real rejection won't fix itself by retrying
                raise RuntimeError("OpenAlex rejected request: " + last + "\n  url: " + url)
            print("  retry %d after %s" % (attempt + 1, last), flush=True)
            time.sleep(3.0 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last = str(e)
            if attempt == tries - 1:
                raise
            print("  retry %d after %s" % (attempt + 1, last), flush=True)
            time.sleep(3.0 * (attempt + 1))
    raise RuntimeError(last or "unreachable")


def authors_of(work):
    names = [a.get("author", {}).get("display_name", "")
             for a in (work.get("authorships") or [])]
    names = [n for n in names if n][:3]
    if len(work.get("authorships") or []) > 3:
        return ", ".join(names) + " et al."
    return ", ".join(names) or "unknown"


def pdf_of(work):
    loc = work.get("best_oa_location") or {}
    return loc.get("pdf_url") or None


def main():
    fj = get(BASE + "/fields?per-page=50")
    fields = fj.get("results") or []
    if not fields:
        raise RuntimeError("OpenAlex /fields returned no results: " + json.dumps(fj)[:200])
    fields.sort(key=lambda f: f.get("id", ""))
    subjects = [f["display_name"].lower() for f in fields]
    print("fields: %d -> %s" % (len(fields), ", ".join(subjects[:5]) + " ..."), flush=True)

    con = sqlite3.connect(OUT)
    con.executescript(
        "PRAGMA journal_mode=WAL;"
        "CREATE TABLE IF NOT EXISTS works("
        " si INTEGER, rank INTEGER, id TEXT, title TEXT, authors TEXT,"
        " year INTEGER, cited INTEGER, pdf TEXT, url TEXT,"
        " PRIMARY KEY(si, rank));"
        "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);")

    for si, f in enumerate(fields):
        have = con.execute("SELECT COUNT(*) FROM works WHERE si=?", (si,)).fetchone()[0]
        if have >= PER_FIELD:
            print("hall %2d %-28s complete (%d)" % (si, subjects[si], have), flush=True)
            continue
        con.execute("DELETE FROM works WHERE si=?", (si,))  # partial halls rebuild whole
        con.commit()
        fid = f["id"].rsplit("/", 1)[-1]
        cursor, rank = "*", 0
        while rank < PER_FIELD:
            url = (BASE + "/works?filter=primary_topic.field.id:" + fid +
                   "&sort=cited_by_count:desc&per-page=200"
                   "&cursor=" + urllib.parse.quote(cursor) +
                   "&select=" + SELECT +
                   (("&mailto=" + urllib.parse.quote(MAILTO)) if MAILTO else ""))
            j = get(url)
            results = j.get("results") or []
            if not results:
                break
            rows = []
            for w in results:
                if rank >= PER_FIELD:
                    break
                rows.append((si, rank, w.get("id", ""),
                             (w.get("display_name") or "Untitled")[:240],
                             authors_of(w)[:160],
                             w.get("publication_year"),
                             w.get("cited_by_count") or 0,
                             pdf_of(w),
                             w.get("doi") or w.get("id")))
                rank += 1
            con.executemany("INSERT OR REPLACE INTO works VALUES(?,?,?,?,?,?,?,?,?)", rows)
            con.commit()
            cursor = (j.get("meta") or {}).get("next_cursor")
            if not cursor:
                break
            time.sleep(0.12)
        print("hall %2d %-28s shelved %d" % (si, subjects[si], rank), flush=True)

    version = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M")
    # per-subject title counts (si -> subject name) for the directory's counts/hiding
    counts = {}
    for si, cnt in con.execute("SELECT si, COUNT(*) FROM works GROUP BY si").fetchall():
        if 0 <= si < len(subjects):
            counts[subjects[si]] = cnt
    for k, v in (("kind", "research"), ("subjects", json.dumps(subjects)),
                 ("subject_counts", json.dumps(counts)),
                 ("built", version[:8]), ("version", version)):
        con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM works").fetchone()[0]
    con.execute("PRAGMA optimize")
    con.close()
    print("research wing: %d works across %d halls -> %s" % (n, len(subjects), OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
