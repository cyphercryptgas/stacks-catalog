#!/usr/bin/env python3
"""Append statute halls to the law wing: the U.S. Code and the CFR.

Each title of the United States Code (1..54) and each title of the Code of
Federal Regulations (1..50) becomes one hall, appended after the court halls
already in law.db. Within a title, sections shelve in code order - so the
opening provisions stand by the door and the deep subsections wait below.

This APPENDS: it reads the halls already present, adds statute halls at the
next free si, and rewrites the merged subjects list. Court halls are untouched.

Env:
  GOVINFO_BASE   default https://api.govinfo.gov
  GOVINFO_KEY    api.data.gov key (DEMO_KEY works but is heavily rate-limited)
  USCODE_YEAR    edition year to pull (default: latest the API offers)
  PER_TITLE      sections per title (default 1920 = 3 rooms of 640)
  WHICH          "both" | "uscode" | "cfr" | "plaw"  (default both)
  OUT            law.db path (default law.db)
"""
import datetime
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

BASE = os.environ.get("GOVINFO_BASE", "https://api.govinfo.gov").rstrip("/")
KEY = os.environ.get("GOVINFO_KEY", "DEMO_KEY")
PER_TITLE = int(os.environ.get("PER_TITLE", "1920"))
WHICH = os.environ.get("WHICH", "both").lower()
MAX_TITLES = int(os.environ.get("MAX_TITLES", "0"))  # 0 = all
OUT = os.environ.get("OUT", "law.db")

# 54 positive-law + non-positive-law titles of the U.S. Code
USC_TITLES = {
    1: "General Provisions", 2: "The Congress", 3: "The President",
    4: "Flag and Seal, Seat of Government, and the States",
    5: "Government Organization and Employees", 6: "Domestic Security",
    7: "Agriculture", 8: "Aliens and Nationality", 9: "Arbitration",
    10: "Armed Forces", 11: "Bankruptcy", 12: "Banks and Banking",
    13: "Census", 14: "Coast Guard", 15: "Commerce and Trade",
    16: "Conservation", 17: "Copyrights", 18: "Crimes and Criminal Procedure",
    19: "Customs Duties", 20: "Education", 21: "Food and Drugs",
    22: "Foreign Relations and Intercourse", 23: "Highways",
    24: "Hospitals and Asylums", 25: "Indians", 26: "Internal Revenue Code",
    27: "Intoxicating Liquors", 28: "Judiciary and Judicial Procedure",
    29: "Labor", 30: "Mineral Lands and Mining", 31: "Money and Finance",
    32: "National Guard", 33: "Navigation and Navigable Waters",
    34: "Crime Control and Law Enforcement", 35: "Patents",
    36: "Patriotic and National Observances, Ceremonies, and Organizations",
    37: "Pay and Allowances of the Uniformed Services",
    38: "Veterans' Benefits", 39: "Postal Service", 40: "Public Buildings",
    41: "Public Contracts", 42: "The Public Health and Welfare",
    43: "Public Lands", 44: "Public Printing and Documents",
    45: "Railroads", 46: "Shipping", 47: "Telecommunications",
    48: "Territories and Insular Possessions", 49: "Transportation",
    50: "War and National Defense", 51: "National and Commercial Space Programs",
    52: "Voting and Elections", 53: "(reserved)",
    54: "National Park Service and Related Programs",
}


def get_json(url, tries=5):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "hexadecagon-library-wing-builder",
                "Accept": "application/json"})
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
                raise RuntimeError("govinfo rejected: " + last + "\n  " + url)
            print("  retry %d after %s" % (attempt + 1, last), flush=True)
            time.sleep(4.0 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last = str(e)
            if attempt == tries - 1:
                raise
            print("  retry %d after %s" % (attempt + 1, last), flush=True)
            time.sleep(4.0 * (attempt + 1))
    raise RuntimeError(last or "unreachable")


def k(url):
    sep = "&" if "?" in url else "?"
    return url + sep + "api_key=" + urllib.parse.quote(KEY)


def latest_uscode_year():
    # collections listing: newest USCODE packages first
    try:
        j = get_json(k(BASE + "/collections/USCODE/2000-01-01T00:00:00Z?pageSize=10&offsetMark=*"))
        years = []
        for p in (j.get("packages") or []):
            pid = p.get("packageId", "")  # USCODE-2023-title5
            bits = pid.split("-")
            if len(bits) >= 2 and bits[1].isdigit():
                years.append(int(bits[1]))
        if years:
            return str(max(years))
    except Exception as e:  # noqa: BLE001
        print("  (year probe failed: %s)" % e, flush=True)
    return os.environ.get("USCODE_YEAR", "2023")


def uscode_sections(year, title, cap):
    """Walk the granules of one USCODE title package; each section is a row."""
    pid = "USCODE-%s-title%d" % (year, title)
    rows, offset = [], "*"
    while len(rows) < cap:
        url = k(BASE + "/packages/" + pid + "/granules?pageSize=100&offsetMark=" +
                urllib.parse.quote(offset))
        try:
            j = get_json(url)
        except RuntimeError:
            break  # title package may not exist this edition
        gr = j.get("granules") or []
        if not gr:
            break
        for g in gr:
            if len(rows) >= cap:
                break
            gid = g.get("granuleId", "")
            title_txt = g.get("title") or gid
            rows.append((gid, title_txt))
        offset = j.get("nextPage") and j.get("offsetMark") or j.get("nextOffsetMark")
        nxt = j.get("nextPage")
        if not nxt:
            break
        # the API returns an offsetMark for the next page inside nextPage url
        try:
            q = urllib.parse.urlparse(nxt).query
            offset = urllib.parse.parse_qs(q).get("offsetMark", ["*"])[0]
        except Exception:  # noqa: BLE001
            break
        time.sleep(0.15)
    return rows


def cfr_sections(year, title, cap):
    """Walk granules across all volumes of one CFR title; each section a row."""
    rows = []
    # CFR titles are split into volumes: CFR-{year}-title{T}-vol{V}
    for vol in range(1, 30):
        if len(rows) >= cap:
            break
        pid = "CFR-%s-title%d-vol%d" % (year, title, vol)
        offset, got_any = "*", False
        while len(rows) < cap:
            url = k(BASE + "/packages/" + pid + "/granules?pageSize=100&offsetMark=" +
                    urllib.parse.quote(offset))
            try:
                j = get_json(url)
            except RuntimeError:
                break  # this volume number doesn't exist; try the next
            gr = j.get("granules") or []
            if not gr:
                break
            got_any = True
            for g in gr:
                if len(rows) >= cap:
                    break
                gid = g.get("granuleId", "")
                rows.append((gid, (g.get("title") or gid)))
            nxt = j.get("nextPage")
            if not nxt:
                break
            try:
                q = urllib.parse.urlparse(nxt).query
                offset = urllib.parse.parse_qs(q).get("offsetMark", ["*"])[0]
            except Exception:  # noqa: BLE001
                break
            time.sleep(0.15)
        if not got_any and vol > 1:
            break  # no more volumes for this title
    return rows


def plaw_for_congress(congress, cap):
    """Public laws of one Congress, newest law number first."""
    rows = []
    url = k(BASE + "/collections/PLAW/2000-01-01T00:00:00Z?pageSize=100&offsetMark=*" +
            "&congress=" + str(congress))
    offset = "*"
    while len(rows) < cap:
        u = k(BASE + "/collections/PLAW/2000-01-01T00:00:00Z?pageSize=100&offsetMark=" +
              urllib.parse.quote(offset) + "&congress=" + str(congress))
        try:
            j = get_json(u)
        except RuntimeError:
            break
        pkgs = j.get("packages") or []
        if not pkgs:
            break
        for p in pkgs:
            if len(rows) >= cap:
                break
            pid = p.get("packageId", "")  # PLAW-119publ9
            if "priv" in pid:  # skip private laws
                continue
            title = p.get("title") or pid
            m = pid.replace("PLAW-", "").replace("publ", " Pub. L. ")
            rows.append((pid, ("Pub. L. " + pid.split("publ")[-1] + " \u2014 " + title)[:240]))
        offset = j.get("nextPage") and "" or None
        nxt = j.get("nextPage")
        if not nxt:
            break
        try:
            q = urllib.parse.urlparse(nxt).query
            offset = urllib.parse.parse_qs(q).get("offsetMark", ["*"])[0]
        except Exception:  # noqa: BLE001
            break
        time.sleep(0.15)
    return rows


def main():
    con = sqlite3.connect(OUT)
    cur = con.cursor()
    # existing halls and subjects
    have = cur.execute("SELECT COALESCE(MAX(si), -1) FROM works").fetchone()[0]
    try:
        subjects = json.loads(cur.execute(
            "SELECT v FROM meta WHERE k='subjects'").fetchone()[0])
    except Exception:  # noqa: BLE001
        subjects = []
    next_si = max(have + 1, len(subjects))
    print("law.db has %d halls; appending statutes from si=%d" % (len(subjects), next_si),
          flush=True)

    year = latest_uscode_year()
    print("U.S. Code edition: %s" % year, flush=True)

    plan = []
    if WHICH in ("both", "uscode"):
        for t in sorted(USC_TITLES):
            if t == 53:
                continue  # reserved/empty
            plan.append(("usc", year, t, "u.s. code \u00b7 title %d \u2014 %s" %
                         (t, USC_TITLES[t].lower())))
    if WHICH in ("both", "cfr"):
        for t in range(1, 51):
            plan.append(("cfr", year, t, "cfr \u00b7 title %d" % t))
    if WHICH in ("both", "plaw"):
        # recent Congresses of public laws; each Congress is a hall
        congresses = [int(c) for c in
                      os.environ.get("PLAW_CONGRESSES", "119,118,117,116,115").split(",")]
        for c in congresses:
            plan.append(("plaw", c, c, "public laws \u00b7 %dth congress" % c))

    if MAX_TITLES > 0:
        plan = plan[:MAX_TITLES]
    for kind, yr, title, label in plan:
        si = next_si
        # idempotent re-run: skip a hall already filled
        n_have = cur.execute("SELECT COUNT(*) FROM works WHERE si=?", (si,)).fetchone()[0]
        if n_have >= 1 and si < len(subjects) and subjects[si] == label:
            print("hall %2d %-44s present (%d)" % (si, label[:44], n_have), flush=True)
            next_si += 1
            continue
        rows = []
        if kind == "usc":
            secs = uscode_sections(yr, title, PER_TITLE)
            pkg = "USCODE-%s-title%d" % (yr, title)
            for rank, (gid, txt) in enumerate(secs):
                # granule details page resolves; whole-title content htm also exists as a fallback
                detail = "https://www.govinfo.gov/app/details/%s/%s" % (pkg, gid)
                content = "https://www.govinfo.gov/content/pkg/%s/html/%s.htm" % (pkg, pkg)
                rows.append((si, rank, gid, txt[:240],
                             "U.S. Code \u00b7 Title %d" % title, yr, 0, content, detail))
        elif kind == "cfr":
            secs = cfr_sections(yr, title, PER_TITLE)
            base_url = "https://www.govinfo.gov/app/collection/CFR/%s/title-%d" % (yr, title)
            for rank, (gid, txt) in enumerate(secs):
                # gid like CFR-2023-title1-vol1-sec1-1 -> package is up to the vol
                pkg = "-".join(gid.split("-")[:4]) if "-vol" in gid else gid
                detail = "https://www.govinfo.gov/app/details/%s/%s" % (pkg, gid)
                rows.append((si, rank, gid, txt[:240],
                             "CFR \u00b7 Title %d" % title, yr, 0, None, detail))
            if not secs:
                rows.append((si, 0, "CFR-%s-title%d" % (yr, title),
                             "Code of Federal Regulations \u2014 Title %d" % title,
                             "CFR \u00b7 Title %d" % title, yr, 0, None, base_url))
        else:  # plaw — public laws of a Congress
            laws = plaw_for_congress(title, PER_TITLE)
            for rank, (pid, txt) in enumerate(laws):
                detail = "https://www.govinfo.gov/app/details/" + pid
                content = "https://www.govinfo.gov/content/pkg/%s/html/%s.htm" % (pid, pid)
                rows.append((si, rank, pid, txt,
                             "%dth Congress" % title, None, 0, content, detail))
        if not rows:
            print("hall %2d %-44s EMPTY (no packages)" % (si, label[:44]), flush=True)
            # still register the hall so numbering stays stable
        cur.execute("DELETE FROM works WHERE si=?", (si,))
        cur.executemany("INSERT OR REPLACE INTO works VALUES(?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        if si < len(subjects):
            subjects[si] = label
        else:
            subjects.append(label)
        print("hall %2d %-44s shelved %d" % (si, label[:44], len(rows)), flush=True)
        next_si += 1
        time.sleep(0.1)

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
