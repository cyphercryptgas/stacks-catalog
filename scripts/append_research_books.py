#!/usr/bin/env python3
"""Append book halls to the research wing: open textbooks and academic books.

Two new sets of halls, appended after the OpenAlex field halls already in
research.db:

  * Open textbooks  - OpenStax's free, peer-reviewed college textbooks, one
    hall ("open textbooks"), every title full-text PDF.
  * Academic books  - DOAB (Directory of Open Access Books): ~90k peer-reviewed
    scholarly books, grouped into halls by subject classification, each book a
    free full-text download.

APPENDS in place: reads existing halls, adds these at the next free si, and
rewrites the merged subjects list. OpenAlex halls are untouched.

Env:
  DOAB_BASE       default https://directory.doabooks.org
  PER_SUBJECT     DOAB books per subject hall (default 640 = 1 room)
  DOAB_SUBJECTS   comma list to override the default subject set
  WHICH           "both" | "textbooks" | "doab"  (default both)
  OUT             research.db path (default research.db)
"""
import datetime
import json
import os
import sqlite3
import sys
import time
import urllib.parse
import urllib.request

DOAB = os.environ.get("DOAB_BASE", "https://directory.doabooks.org").rstrip("/")
PER_SUBJECT = int(os.environ.get("PER_SUBJECT", "640"))
WHICH = os.environ.get("WHICH", "both").lower()
OUT = os.environ.get("OUT", "research.db")

# DOAB subject classifications -> one hall each (the rare-deep gradient is by
# recency within a subject, newest by the door).
DEFAULT_SUBJECTS = [
    "Science: general issues",
    "Mathematics",
    "Physics",
    "Chemistry",
    "Biology, life sciences",
    "Earth sciences, geography, environment, planning",
    "Technology, engineering, agriculture",
    "Medicine",
    "Computer science",
    "Economics, finance, business and management",
    "Society and social sciences",
    "Politics and government",
    "Law",
    "Philosophy",
    "History",
    "Language and linguistics",
    "Literature and literary studies",
    "The arts",
    "Education",
    "Religion and beliefs",
]


def get_json(url, headers=None, tries=5):
    last = None
    h = {"User-Agent": "hexadecagon-library-wing-builder", "Accept": "application/json"}
    if headers:
        h.update(headers)
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=h)
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
                raise RuntimeError("rejected: " + last + "\n  " + url)
            print("  retry %d after %s" % (attempt + 1, last), flush=True)
            time.sleep(4.0 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            last = str(e)
            if attempt == tries - 1:
                raise
            print("  retry %d after %s" % (attempt + 1, last), flush=True)
            time.sleep(4.0 * (attempt + 1))
    raise RuntimeError(last or "unreachable")


def meta_val(item, key):
    for m in item.get("metadata", []):
        if m.get("key") == key:
            return m.get("value")
    return None


def meta_all(item, key):
    return [m.get("value") for m in item.get("metadata", []) if m.get("key") == key]


def pdf_of(item):
    for b in item.get("bitstreams", []):
        name = (b.get("bundleName") or "") + (b.get("mimeType") or "")
        if "pdf" in (b.get("mimeType") or "").lower() or \
           (b.get("name") or "").lower().endswith(".pdf"):
            link = b.get("retrieveLink") or b.get("link")
            if link:
                return DOAB + link if link.startswith("/") else link
    return None


def doab_subject(subject, cap):
    """Newest-first books for one DOAB subject classification."""
    q = ('dc.subject.classification:"%s"' % subject)
    url = (DOAB + "/rest/search?query=" + urllib.parse.quote(q) +
           "&sort=dc.date.accessioned_dt&order=desc&expand=metadata,bitstreams" +
           "&limit=" + str(min(cap, 100)) + "&offset=0")
    rows, offset = [], 0
    while len(rows) < cap:
        u = (DOAB + "/rest/search?query=" + urllib.parse.quote(q) +
             "&expand=metadata,bitstreams&limit=" + str(min(cap - len(rows), 100)) +
             "&offset=" + str(offset))
        try:
            items = get_json(u)
        except RuntimeError:
            break
        if not isinstance(items, list) or not items:
            break
        for it in items:
            if len(rows) >= cap:
                break
            title = meta_val(it, "dc.title") or "Untitled"
            authors = meta_all(it, "dc.contributor.author")[:3]
            authors = ", ".join(a for a in authors if a) or "various"
            ystr = (meta_val(it, "dc.date.issued") or "")[:4]
            year = int(ystr) if ystr.isdigit() else None
            handle = it.get("handle") or it.get("uuid") or title
            url_page = "https://directory.doabooks.org/handle/" + handle if it.get("handle") \
                else (meta_val(it, "oapen.identifier.doi") or "")
            rows.append((handle, title[:240], authors[:160], year,
                         pdf_of(it), url_page))
        offset += len(items)
        if len(items) < 100:
            break
        time.sleep(0.2)
    return rows


# A compact, stable set of well-known OpenStax titles with their canonical PDF
# landing pages. (OpenStax has no list API; these are its flagship catalog.)
OPENSTAX = [
    ("College Physics 2e", "https://openstax.org/details/books/college-physics-2e"),
    ("University Physics Volume 1", "https://openstax.org/details/books/university-physics-volume-1"),
    ("University Physics Volume 2", "https://openstax.org/details/books/university-physics-volume-2"),
    ("University Physics Volume 3", "https://openstax.org/details/books/university-physics-volume-3"),
    ("Chemistry 2e", "https://openstax.org/details/books/chemistry-2e"),
    ("Chemistry: Atoms First 2e", "https://openstax.org/details/books/chemistry-atoms-first-2e"),
    ("Biology 2e", "https://openstax.org/details/books/biology-2e"),
    ("Concepts of Biology", "https://openstax.org/details/books/concepts-biology"),
    ("Anatomy and Physiology 2e", "https://openstax.org/details/books/anatomy-and-physiology-2e"),
    ("Microbiology", "https://openstax.org/details/books/microbiology"),
    ("Calculus Volume 1", "https://openstax.org/details/books/calculus-volume-1"),
    ("Calculus Volume 2", "https://openstax.org/details/books/calculus-volume-2"),
    ("Calculus Volume 3", "https://openstax.org/details/books/calculus-volume-3"),
    ("Precalculus 2e", "https://openstax.org/details/books/precalculus-2e"),
    ("Algebra and Trigonometry 2e", "https://openstax.org/details/books/algebra-and-trigonometry-2e"),
    ("College Algebra 2e", "https://openstax.org/details/books/college-algebra-2e"),
    ("Introductory Statistics", "https://openstax.org/details/books/introductory-statistics"),
    ("Introductory Business Statistics", "https://openstax.org/details/books/introductory-business-statistics"),
    ("Principles of Economics 3e", "https://openstax.org/details/books/principles-economics-3e"),
    ("Principles of Macroeconomics 3e", "https://openstax.org/details/books/principles-macroeconomics-3e"),
    ("Principles of Microeconomics 3e", "https://openstax.org/details/books/principles-microeconomics-3e"),
    ("Psychology 2e", "https://openstax.org/details/books/psychology-2e"),
    ("Introduction to Sociology 3e", "https://openstax.org/details/books/introduction-sociology-3e"),
    ("American Government 3e", "https://openstax.org/details/books/american-government-3e"),
    ("U.S. History", "https://openstax.org/details/books/us-history"),
    ("Principles of Accounting Volume 1", "https://openstax.org/details/books/principles-financial-accounting"),
    ("Organizational Behavior", "https://openstax.org/details/books/organizational-behavior"),
    ("Introduction to Philosophy", "https://openstax.org/details/books/introduction-philosophy"),
    ("Writing Guide with Handbook", "https://openstax.org/details/books/writing-guide"),
    ("Astronomy 2e", "https://openstax.org/details/books/astronomy-2e"),
]


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
    print("research.db has %d halls; appending books from si=%d" % (len(subjects), next_si),
          flush=True)

    def register(si, label, rows):
        cur.execute("DELETE FROM works WHERE si=?", (si,))
        cur.executemany("INSERT OR REPLACE INTO works VALUES(?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
        if si < len(subjects):
            subjects[si] = label
        else:
            subjects.append(label)
        print("hall %2d %-34s shelved %d" % (si, label, len(rows)), flush=True)

    # --- open textbooks (one hall) ---
    if WHICH in ("both", "textbooks"):
        si = next_si
        rows = []
        for rank, (title, page) in enumerate(OPENSTAX):
            pdf = page.rstrip("/") + "/pdf"  # OpenStax detail pages expose a /pdf
            rows.append((si, rank, "openstax:" + str(rank), title, "OpenStax",
                         None, 0, pdf, page))
        register(si, "open textbooks", rows)
        next_si += 1

    # --- DOAB academic books (one hall per subject) ---
    if WHICH in ("both", "doab"):
        subs = os.environ.get("DOAB_SUBJECTS")
        subs = [s.strip() for s in subs.split(",")] if subs else DEFAULT_SUBJECTS
        for subject in subs:
            si = next_si
            label = subject.lower()
            n_have = cur.execute("SELECT COUNT(*) FROM works WHERE si=?", (si,)).fetchone()[0]
            if n_have >= 1 and si < len(subjects) and subjects[si] == label:
                print("hall %2d %-34s present (%d)" % (si, label, n_have), flush=True)
                next_si += 1
                continue
            try:
                books = doab_subject(subject, PER_SUBJECT)
            except Exception as e:  # noqa: BLE001
                print("hall %2d %-34s ERROR %s" % (si, label, e), flush=True)
                books = []
            rows = [(si, rank, h, t, a, y, 0, pdf, page)
                    for rank, (h, t, a, y, pdf, page) in enumerate(books)]
            register(si, label, rows)
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
    print("research wing now: %d rows across %d halls -> %s" % (total, len(subjects), OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
