#!/usr/bin/env python3
"""Build the Law Library index: the most-cited opinions per court.

Sixteen courts become the wing's halls - the Supreme Court, the thirteen
federal circuits, and two storied state high courts. Within a hall, opinions
shelve by how often they're cited: Marbury by the door, the forgotten below.

Env:
  COURTLISTENER_BASE  default https://www.courtlistener.com
  CL_TOKEN            CourtListener API token (optional; raises rate limits)
  PER_COURT           opinions per court (default 2560 = 4 rooms of 640)
  OUT                 output sqlite path (default law.db)
"""
import datetime
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

BASE = os.environ.get("COURTLISTENER_BASE", "https://www.courtlistener.com").rstrip("/")
TOKEN = os.environ.get("CL_TOKEN", "")
PER_COURT = int(os.environ.get("PER_COURT", "2560"))
# pre-cache full text for the top-N most-cited opinions per court (instant reads,
# no live rate-limit). 0 disables. ~13s pacing keeps under 5 requests/minute.
PRECACHE = int(os.environ.get("PRECACHE", "40"))
PRECACHE_PAUSE = float(os.environ.get("PRECACHE_PAUSE", "13"))
OUT = os.environ.get("OUT", "law.db")

COURTS = [
    ("scotus", "Supreme Court of the United States"),
    ("ca1", "First Circuit"), ("ca2", "Second Circuit"),
    ("ca3", "Third Circuit"), ("ca4", "Fourth Circuit"),
    ("ca5", "Fifth Circuit"), ("ca6", "Sixth Circuit"),
    ("ca7", "Seventh Circuit"), ("ca8", "Eighth Circuit"),
    ("ca9", "Ninth Circuit"), ("ca10", "Tenth Circuit"),
    ("ca11", "Eleventh Circuit"), ("cadc", "D.C. Circuit"),
    ("cafc", "Federal Circuit"),
    # state high courts — the most-cited state supreme courts
    ("cal", "Supreme Court of California"),
    ("ny", "New York Court of Appeals"),
    ("tex", "Supreme Court of Texas"),
    ("fla", "Supreme Court of Florida"),
    ("ill", "Supreme Court of Illinois"),
    ("mass", "Supreme Judicial Court of Massachusetts"),
    ("pa", "Supreme Court of Pennsylvania"),
    ("ohio", "Supreme Court of Ohio"),
    ("nj", "Supreme Court of New Jersey"),
    ("wash", "Supreme Court of Washington"),
    ("mich", "Supreme Court of Michigan"),
    ("ga", "Supreme Court of Georgia"),
    ("va", "Supreme Court of Virginia"),
    ("nc", "Supreme Court of North Carolina"),
]


def get(url, tries=5):
    last = None
    for attempt in range(tries):
        try:
            headers = {"User-Agent": "hexadecagon-library-wing-builder"}
            if TOKEN:
                headers["Authorization"] = "Token " + TOKEN
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=90) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8")[:300]
            except Exception:  # noqa: BLE001
                pass
            last = "HTTP %s %s" % (e.code, body)
            if e.code in (400, 401, 403, 404):
                raise RuntimeError("CourtListener rejected request: " + last + "\n  url: " + url)
            print("  retry %d after %s" % (attempt + 1, last), flush=True)
            time.sleep(4.0 * (attempt + 1))  # 429s want patience
        except Exception as e:  # noqa: BLE001
            last = str(e)
            if attempt == tries - 1:
                raise
            print("  retry %d after %s" % (attempt + 1, last), flush=True)
            time.sleep(4.0 * (attempt + 1))
    raise RuntimeError(last or "unreachable")


def clean_opinion_html(html):
    import re
    t = re.sub(r"</(p|div|h\d|blockquote)>", "\n\n", html, flags=re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = (t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
         .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def opinion_text(op_id):
    """Fetch one opinion's full text (cluster fallback). Returns text or ''."""
    try:
        j = get(BASE + "/api/rest/v4/opinions/%s/?format=json" % op_id)
    except Exception:  # noqa: BLE001
        return ""
    txt = j.get("plain_text") or ""
    if not txt:
        for key in ("html_with_citations", "html", "html_lawbox", "html_columbia"):
            if j.get(key):
                txt = clean_opinion_html(j[key])
                if txt:
                    break
    return (txt or "")[:600000]


def main():
    subjects = [label.lower() for _, label in COURTS]
    con = sqlite3.connect(OUT)
    con.executescript(
        "PRAGMA journal_mode=WAL;"
        "CREATE TABLE IF NOT EXISTS works("
        " si INTEGER, rank INTEGER, id TEXT, title TEXT, authors TEXT,"
        " year INTEGER, cited INTEGER, pdf TEXT, url TEXT,"
        " PRIMARY KEY(si, rank));"
        "CREATE TABLE IF NOT EXISTS bodies(id TEXT PRIMARY KEY, text TEXT);"
        "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);")

    pause = 0.2 if TOKEN else 0.8
    for si, (cid, label) in enumerate(COURTS):
        have = con.execute("SELECT COUNT(*) FROM works WHERE si=?", (si,)).fetchone()[0]
        if have >= PER_COURT:
            print("hall %2d %-34s complete (%d)" % (si, label, have), flush=True)
            continue
        con.execute("DELETE FROM works WHERE si=?", (si,))
        con.commit()
        url = (BASE + "/api/rest/v4/search/?type=o&court=" + cid +
               "&order_by=" + urllib.parse.quote("citeCount desc"))
        rank = 0
        try:
          while rank < PER_COURT and url:
            j = get(url)
            results = j.get("results") or []
            if not results:
                break
            rows = []
            for r in results:
                if rank >= PER_COURT:
                    break
                op = (r.get("opinions") or [{}])[0]
                year = None
                df = r.get("dateFiled") or r.get("date_filed") or ""
                if len(df) >= 4 and df[:4].isdigit():
                    year = int(df[:4])
                rows.append((si, rank,
                             str(op.get("id") or ""),
                             (r.get("caseName") or r.get("case_name") or "Untitled case")[:240],
                             label,
                             year,
                             r.get("citeCount") or r.get("cite_count") or 0,
                             None,
                             (BASE + r["absolute_url"]) if r.get("absolute_url") else None))
                rank += 1
            con.executemany("INSERT OR REPLACE INTO works VALUES(?,?,?,?,?,?,?,?,?)", rows)
            con.commit()
            url = j.get("next")
            time.sleep(pause)
        except Exception as e:  # noqa: BLE001 - a court may 500 or rate-limit; keep the others
            print("hall %2d %-34s ERROR %s (shelved %d so far)" % (si, label, e, rank), flush=True)
        print("hall %2d %-34s shelved %d" % (si, label, rank), flush=True)

        # pre-cache full text for the most-cited opinions in this court, so the
        # famous cases read instantly from the DB and never hit the rate limit.
        if PRECACHE > 0:
            top = con.execute(
                "SELECT id FROM works WHERE si=? AND id!='' ORDER BY rank LIMIT ?",
                (si, PRECACHE)).fetchall()
            done = 0
            for (op_id,) in top:
                if con.execute("SELECT 1 FROM bodies WHERE id=?", (op_id,)).fetchone():
                    done += 1
                    continue
                txt = opinion_text(op_id)
                if txt:
                    con.execute("INSERT OR REPLACE INTO bodies VALUES(?,?)", (op_id, txt))
                    done += 1
                # rate-limit-aware: CourtListener allows ~5/min on a free token
                time.sleep(PRECACHE_PAUSE)
            con.commit()
            print("hall %2d %-34s pre-cached %d/%d texts" % (si, label, done, len(top)), flush=True)

    version = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M")
    for k, v in (("kind", "law"), ("subjects", json.dumps(subjects)),
                 ("built", version[:8]), ("version", version)):
        con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, v))
    con.commit()
    n = con.execute("SELECT COUNT(*) FROM works").fetchone()[0]
    con.execute("PRAGMA optimize")
    con.close()
    print("law library: %d opinions across %d courts -> %s" % (n, len(COURTS), OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
