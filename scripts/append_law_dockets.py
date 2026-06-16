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

# Rate-limit awareness (same approach as build_wing_law.py): CourtListener's
# free tier allows only ~50/hour, 125/day. REQUEST_BUDGET caps API calls per run
# so we bank what we get and STOP cleanly instead of retry-grinding for ~30min
# against the 50/hour wall (which is what made past runs take hours). Already-
# filled docket halls are skipped, so re-running fills more halls across days.
REQUEST_BUDGET = int(os.environ.get("REQUEST_BUDGET", "90"))
_req_count = {"n": 0}


class BudgetExhausted(Exception):
    """Raised when the per-run API request budget is spent — stop cleanly."""


class RateLimited(Exception):
    """Raised when CourtListener returns 429 after a short retry — stop for today."""

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
    if _req_count["n"] >= REQUEST_BUDGET:
        raise BudgetExhausted("hit REQUEST_BUDGET of %d calls" % REQUEST_BUDGET)
    _req_count["n"] += 1
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
            if e.code == 429:
                # honor a SHORT Retry-After once (the 5/min limit clears fast),
                # but never wait on the 50/hour wall (~1800s) — bank progress and
                # stop the run so we don't burn 30+ minutes sleeping.
                ra = 0
                try:
                    ra = int(e.headers.get("Retry-After") or 0)
                except Exception:  # noqa: BLE001
                    ra = 0
                if attempt == 0 and 0 < ra <= 30:
                    print("  429 (5/min); waiting %ds once" % ra, flush=True)
                    time.sleep(ra)
                    continue
                raise RateLimited(last)
            print("  retry %d after %s" % (attempt + 1, last), flush=True)
            time.sleep(4.0 * (attempt + 1))
        except (BudgetExhausted, RateLimited):
            raise
        except Exception as e:  # noqa: BLE001
            last = str(e)
            if attempt == tries - 1:
                raise
            print("  retry %d after %s" % (attempt + 1, last), flush=True)
            time.sleep(4.0 * (attempt + 1))
    raise RuntimeError(last or "unreachable")


def dockets_for(nature, cap):
    """Most-relevant dockets for a nature-of-suit, via type=r RECAP search.

    Returns (rows, stop) where stop is a BudgetExhausted/RateLimited instance if
    the run should halt after banking these rows, else None. Rows gathered before
    a stop are complete and safe to shelve.
    """
    rows = []
    url = (BASE + "/api/rest/v4/search/?type=r&order_by=" +
           urllib.parse.quote("score desc") +
           "&nature_of_suit=" + urllib.parse.quote(nature) +
           "&q=" + urllib.parse.quote(nature))
    pause = 0.3 if TOKEN else 1.0
    while len(rows) < cap and url:
        try:
            j = get(url)
        except (BudgetExhausted, RateLimited) as e:
            return rows, e  # bank what we have, signal halt
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
    return rows, None


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
    FILLED_MIN = int(os.environ.get("FILLED_MIN", "1"))
    stopped = None
    filled_this_run = 0
    for nature, label_h in NATURES:
        si = next_si
        label = "dockets \u00b7 " + label_h.lower()
        n_have = cur.execute("SELECT COUNT(*) FROM works WHERE si=?", (si,)).fetchone()[0]
        if n_have >= FILLED_MIN and si < len(subjects) and subjects[si] == label:
            print("hall %2d %-40s present (%d) — skipping" % (si, label[:40], n_have), flush=True)
            next_si += 1
            continue
        ds, stop = dockets_for(nature, PER_NOS)
        # id field carries "recap:<docket_id>" so the reader knows it's a docket.
        # Only replace the hall's rows if we actually gathered some — never
        # delete-then-leave-empty (that was corrupting halls on a 429).
        if ds:
            rows = [(si, rank, "recap:" + did, case, court, year, 0, None, url_abs)
                    for rank, (did, case, court, year, url_abs) in enumerate(ds)]
            cur.execute("DELETE FROM works WHERE si=?", (si,))
            cur.executemany("INSERT OR REPLACE INTO works VALUES(?,?,?,?,?,?,?,?,?)", rows)
            con.commit()
            if si < len(subjects):
                subjects[si] = label
            else:
                subjects.append(label)
            filled_this_run += 1
            print("hall %2d %-40s shelved %d (requests used: %d/%d)"
                  % (si, label[:40], len(rows), _req_count["n"], REQUEST_BUDGET), flush=True)
        else:
            print("hall %2d %-40s no data (left existing %d intact)"
                  % (si, label[:40], n_have), flush=True)
        next_si += 1
        if stop is not None:
            stopped = stop
            print("  -> stopping run: %s" % stop, flush=True)
            break
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
    # how many docket halls still need filling, so the operator knows to re-run.
    # count by LABEL (subjects starting "dockets ·") rather than guessing the si
    # offset — the docket halls sit after courts+statutes+cfr+public-laws, so an
    # arithmetic offset lands on the wrong halls.
    remaining = 0
    for sidx, subj in enumerate(subjects):
        if isinstance(subj, str) and subj.startswith("dockets \u00b7"):
            c = cur.execute("SELECT COUNT(*) FROM works WHERE si=?", (sidx,)).fetchone()[0]
            if c < FILLED_MIN:
                remaining += 1
    # also count docket halls not yet in subjects at all (never reached this run)
    filled_labels = {s for s in subjects if isinstance(s, str) and s.startswith("dockets \u00b7")}
    for _, label_h in NATURES:
        if ("dockets \u00b7 " + label_h.lower()) not in filled_labels:
            remaining += 1
    con.execute("PRAGMA optimize")
    con.close()
    print("law wing now: %d rows across %d halls -> %s" % (total, len(subjects), OUT))
    print("run summary: filled %d docket hall(s) this run, used %d/%d requests; %d still empty"
          % (filled_this_run, _req_count["n"], REQUEST_BUDGET, remaining), flush=True)
    if stopped is not None or remaining:
        print("  -> run this step again (another hour/day for the 50/hour limit) "
              "to fill the remaining docket hall(s). filled halls are skipped.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
