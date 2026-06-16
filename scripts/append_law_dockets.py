#!/usr/bin/env python3
"""Append RECAP docket halls to the law wing: federal lawsuits by type.

Where the court halls shelve *opinions* (what judges decided) and the statute
halls shelve the *written law*, these halls shelve *dockets* - the cases
themselves, the whole story of a federal lawsuit: parties, the filing history,
the documents that have been freed from PACER.

Each PACER "nature of suit" becomes a hall - Civil Rights, Bankruptcy, Patent,
Criminal, and so on - and within a hall the cases shelve by attention (the
search relevance/score order CourtListener exposes), so the landmark litigation
in each category stands by the door.

APPENDS in place after the court + statute halls already in law.db, merging the
subjects list. Existing halls are untouched.

Env:
  COURTLISTENER_BASE  default https://www.courtlistener.com
  CL_TOKEN            CourtListener API token (membership token recommended)
  PER_NOS             dockets per nature-of-suit hall (default 640 = 1 room)
  OUT                 law.db path (default law.db)
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
PER_NOS = int(os.environ.get("PER_NOS", "640"))
OUT = os.environ.get("OUT", "law.db")

# PACER nature-of-suit groups -> one hall each. The query matches the suit
# label as free text within type=r RECAP search, which is how the front end
# groups them too.
NATURES = [
    ("civil rights", "Civil Rights"),
    ("prisoner", "Prisoner Petitions"),
    ("labor", "Labor"),
    ("contract", "Contract"),
    ("torts", "Personal Injury & Torts"),
    ("intellectual property", "Intellectual Property"),
    ("patent", "Patent"),
    ("copyright", "Copyright & Trademark"),
    ("bankruptcy", "Bankruptcy"),
    ("securities", "Securities & Commodities"),
    ("antitrust", "Antitrust"),
    ("immigration", "Immigration"),
    ("habeas corpus", "Habeas Corpus"),
    ("criminal", "Criminal"),
    ("environmental", "Environmental"),
    ("tax", "Tax"),
    ("social security", "Social Security"),
    ("forfeiture", "Forfeiture & Penalty"),
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
                body = e.read().decode("utf-8")[:200]
            except Exception:  # noqa: BLE001
                pass
            last = "HTTP %s %s" % (e.code, body)
            if e.code in (400, 401, 403, 404):
                raise RuntimeError("CourtListener rejected: " + last + "\n  " + url)
            print("  retry %d after %s" % (attempt + 1, last), flush=True)
            time.sleep(4.0 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last = str(e)
            if attempt == tries - 1:
                raise
            print("  retry %d after %s" % (attempt + 1, last), flush=True)
            time.sleep(4.0 * (attempt + 1))
    raise RuntimeError(last or "unreachable")


def dockets_for(nature, cap):
    """Most-relevant dockets for a nature-of-suit, via type=r RECAP search."""
    rows = []
    url = (BASE + "/api/rest/v4/search/?type=r&order_by=" +
           urllib.parse.quote("score desc") +
           "&nature_of_suit=" + urllib.parse.quote(nature) +
           "&q=" + urllib.parse.quote(nature))
    pause = 0.3 if TOKEN else 1.0
    while len(rows) < cap and url:
        j = get(url)
        results = j.get("results") or []
        if not results:
            break
        for r in results:
            if len(rows) >= cap:
                break
            did = r.get("docket_id") or r.get("id")
            if not did:
                continue
            case = r.get("caseName") or r.get("case_name") or "Untitled docket"
            court = r.get("court") or r.get("court_id") or ""
            df = r.get("dateFiled") or r.get("date_filed") or ""
            year = int(df[:4]) if len(df) >= 4 and df[:4].isdigit() else None
            dnum = r.get("docketNumber") or r.get("docket_number") or ""
            url_abs = r.get("docket_absolute_url") or r.get("absolute_url") or ""
            rows.append((str(did), case[:240], (court + (" \u00b7 " + dnum if dnum else ""))[:160],
                         year, (BASE + url_abs) if url_abs.startswith("/") else (url_abs or None)))
        url = j.get("next")
        time.sleep(pause)
    return rows


def main():
    con = sqlite3.connect(OUT)
    cur = con.cursor()
    have = cur.execute("SELECT COALESCE(MAX(si), -1) FROM works").fetchone()[0]
    try:
        subjects = json.loads(cur.execute(
            "SELECT v FROM meta WHERE k='subjects'").fetchone()[0])
    except Exception:  # noqa: BLE001
        subjects = []
    next_si = max(have + 1, len(subjects))
    print("law.db has %d halls; appending RECAP dockets from si=%d" % (len(subjects), next_si),
          flush=True)

    for nature, label_h in NATURES:
        si = next_si
        label = "dockets \u00b7 " + label_h.lower()
        n_have = cur.execute("SELECT COUNT(*) FROM works WHERE si=?", (si,)).fetchone()[0]
        if n_have >= 1 and si < len(subjects) and subjects[si] == label:
            print("hall %2d %-40s present (%d)" % (si, label[:40], n_have), flush=True)
            next_si += 1
            continue
        try:
            ds = dockets_for(nature, PER_NOS)
        except Exception as e:  # noqa: BLE001
            print("hall %2d %-40s ERROR %s" % (si, label[:40], e), flush=True)
            ds = []
        # id field carries "recap:<docket_id>" so the reader knows it's a docket
        rows = [(si, rank, "recap:" + did, case, court, year, 0, None, url_abs)
                for rank, (did, case, court, year, url_abs) in enumerate(ds)]
        cur.execute("DELETE FROM works WHERE si=?", (si,))
        cur.executemany("INSERT OR REPLACE INTO works VALUES(?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        if si < len(subjects):
            subjects[si] = label
        else:
            subjects.append(label)
        print("hall %2d %-40s shelved %d" % (si, label[:40], len(rows)), flush=True)
        next_si += 1
        time.sleep(0.2)

    version = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d-%H%M")
    sc = {}
    for si, cnt in cur.execute("SELECT si, COUNT(*) FROM works GROUP BY si").fetchall():
        if 0 <= si < len(subjects):
            sc[subjects[si]] = cnt
    for kk, vv in (("subjects", json.dumps(subjects)),
                   ("subject_counts", json.dumps(sc)),
                   ("built", version[:8]), ("version", version)):
        cur.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (kk, vv))
    con.commit()
    total = cur.execute("SELECT COUNT(*) FROM works").fetchone()[0]
    con.execute("PRAGMA optimize")
    con.close()
    print("law wing now: %d rows across %d halls -> %s" % (total, len(subjects), OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
