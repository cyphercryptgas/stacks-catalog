#!/usr/bin/env python3
"""
build_index.py — Open Library dumps -> compact SQLite catalog index.

Designed for a GitHub Actions runner (7GB RAM / 14GB disk): the editions dump
(~12GB gz) is never held in memory or fully on disk; it streams through an
external sort. Only the author-name map and the per-subject top-N heaps live
in RAM.

Pipeline:
  1. editions dump -> ed.tsv (work, year, ocaid, eng?, gutenberg, cover)
     -> sort by work -> reduce to one aggregate line per work
  2. works dump    -> wk.tsv (work, title, author_key, cover, subject_mask)
     -> sort by work
  3. merge-join 1+2, resolve author names (dict from authors dump),
     keep rows with a title and (cover or scan), insert into SQLite,
     maintain per-subject top-CAP heaps -> subject_rank table
  4. FTS5 index on title+author, meta table, VACUUM
"""
import argparse, gzip, heapq, io, json, os, re, sqlite3, subprocess, sys, tempfile, time

# Must match the front-end SUBJECTS array exactly (order defines `si`).
SUBJECTS = [
    "fiction", "fantasy", "science fiction", "mystery", "romance", "horror",
    "adventure", "historical fiction", "short stories", "poetry", "drama", "humor",
    "classics", "fairy tales", "detective and mystery stories", "westerns",
    "sea stories", "spy stories", "comics", "young adult fiction", "juvenile fiction",
    "children's stories", "biography", "autobiography", "history", "ancient history",
    "world war ii", "philosophy", "ethics", "psychology", "religion", "mythology",
    "folklore", "science", "mathematics", "physics", "chemistry", "biology",
    "astronomy", "geology", "medicine", "natural history", "animals", "birds",
    "nature", "gardening", "cooking", "health", "travel", "geography",
    "exploration", "art", "painting", "photography", "architecture", "music",
    "theater", "education", "language", "law", "politics", "economics",
    "business", "engineering",
]
NORM = re.compile(r"[^a-z0-9 ]+")
YEAR = re.compile(r"(1[5-9]\d\d|20[0-2]\d)")

def norm_subject(s):
    return NORM.sub(" ", s.lower()).strip()

SUBJ_NORM = [norm_subject(s) for s in SUBJECTS]

def subject_mask(subjects):
    mask = 0
    for raw in subjects:
        if not isinstance(raw, str):
            continue
        n = norm_subject(raw)
        for i, canon in enumerate(SUBJ_NORM):
            if n == canon or n.startswith(canon + " "):
                mask |= (1 << i)
    return mask

def open_stream(path):
    """Yield text lines from a local file, .gz file, or http(s) URL (curl|zcat)."""
    if path.startswith("http"):
        p = subprocess.Popen("curl -sL --retry 4 '%s' | zcat" % path,
                             shell=True, stdout=subprocess.PIPE)
        return io.TextIOWrapper(p.stdout, encoding="utf-8", errors="replace"), p
    if path.endswith(".gz"):
        return io.TextIOWrapper(gzip.open(path, "rb"), encoding="utf-8", errors="replace"), None
    return open(path, encoding="utf-8", errors="replace"), None

def dump_json(line):
    """Dump lines are 5 tab-separated columns; JSON is the 5th."""
    parts = line.rstrip("\n").split("\t")
    if len(parts) < 5:
        return None
    try:
        return json.loads(parts[4])
    except Exception:
        return None

def clean(s, limit=300):
    if not isinstance(s, str):
        return ""
    return s.replace("\t", " ").replace("\n", " ").replace("\r", " ").strip()[:limit]

def ext_sort(src, dst, tmpdir):
    env = dict(os.environ, LC_ALL="C")
    subprocess.check_call(
        ["sort", "-t", "\t", "-k1,1", "-S", "1500M", "-T", tmpdir,
         "--compress-program=gzip", "-o", dst, src], env=env)

def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)

# ---------------------------------------------------------------- editions
def pass_editions(path, tmpdir):
    raw = os.path.join(tmpdir, "ed.tsv")
    n = 0
    stream, proc = open_stream(path)
    with open(raw, "w", encoding="utf-8") as out:
        for line in stream:
            d = dump_json(line)
            if not d:
                continue
            works = d.get("works") or []
            wk = works[0].get("key") if works and isinstance(works[0], dict) else None
            if not wk:
                continue
            year = 0
            m = YEAR.search(d.get("publish_date") or "")
            if m:
                year = int(m.group(1))
            ocaid = clean(d.get("ocaid") or "", 80)
            langs = d.get("languages") or []
            eng = 1 if any(isinstance(l, dict) and l.get("key") == "/languages/eng" for l in langs) else 0
            ids = d.get("identifiers") or {}
            gut = ids.get("project_gutenberg") or []
            gut = clean(str(gut[0]), 12) if gut else ""
            covers = d.get("covers") or []
            cov = covers[0] if covers and isinstance(covers[0], int) and covers[0] > 0 else 0
            out.write("%s\t%d\t%s\t%d\t%s\t%d\n" % (wk, year, ocaid, eng, gut, cov))
            n += 1
            if n % 2000000 == 0:
                log("editions scanned: %dM" % (n / 1000000))
    if proc:
        proc.wait()
    log("editions lines: %d — sorting" % n)
    srt = os.path.join(tmpdir, "ed.sorted.tsv")
    ext_sort(raw, srt, tmpdir)
    os.unlink(raw)
    # reduce to one aggregate per work
    agg = os.path.join(tmpdir, "agg.tsv")
    with open(srt, encoding="utf-8") as f, open(agg, "w", encoding="utf-8") as out:
        cur = None; ecount = 0; year = 0; oc_eng = ""; oc_any = ""; gut = ""; cov = 0
        def flush():
            if cur is not None:
                out.write("%s\t%d\t%d\t%s\t%s\t%d\n" %
                          (cur, ecount, year, oc_eng or oc_any, gut, cov))
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) != 6:
                continue
            wk, y, oc, eng, g, cv = p[0], int(p[1] or 0), p[2], p[3] == "1", p[4], int(p[5] or 0)
            if wk != cur:
                flush()
                cur, ecount, year, oc_eng, oc_any, gut, cov = wk, 0, 0, "", "", "", 0
            ecount += 1
            if y and (year == 0 or y < year):
                year = y
            if oc:
                if eng and not oc_eng:
                    oc_eng = oc
                if not oc_any:
                    oc_any = oc
            if g and not gut:
                gut = g
            if cv and not cov:
                cov = cv
        flush()
    os.unlink(srt)
    return agg

# ---------------------------------------------------------------- works
def pass_works(path, tmpdir):
    raw = os.path.join(tmpdir, "wk.tsv")
    n = 0
    stream, proc = open_stream(path)
    with open(raw, "w", encoding="utf-8") as out:
        for line in stream:
            d = dump_json(line)
            if not d:
                continue
            key = d.get("key") or ""
            title = clean(d.get("title") or "", 240)
            if not key or not title:
                continue
            authors = d.get("authors") or []
            ak = ""
            if authors and isinstance(authors[0], dict):
                a = authors[0].get("author")
                if isinstance(a, dict):
                    ak = a.get("key") or ""
            covers = d.get("covers") or []
            cov = covers[0] if covers and isinstance(covers[0], int) and covers[0] > 0 else 0
            mask = subject_mask(d.get("subjects") or [])
            out.write("%s\t%s\t%s\t%d\t%d\n" % (key, title, ak, cov, mask))
            n += 1
            if n % 2000000 == 0:
                log("works scanned: %dM" % (n / 1000000))
    if proc:
        proc.wait()
    log("works lines: %d — sorting" % n)
    srt = os.path.join(tmpdir, "wk.sorted.tsv")
    ext_sort(raw, srt, tmpdir)
    os.unlink(raw)
    return srt

# ---------------------------------------------------------------- authors
def load_authors(path):
    names = {}
    stream, proc = open_stream(path)
    n = 0
    for line in stream:
        d = dump_json(line)
        if not d:
            continue
        k = d.get("key") or ""
        nm = clean(d.get("name") or "", 80)
        if k and nm:
            names[k] = nm
        n += 1
        if n % 2000000 == 0:
            log("authors scanned: %dM (kept %d)" % (n / 1000000, len(names)))
    if proc:
        proc.wait()
    log("author names: %d" % len(names))
    return names

# ---------------------------------------------------------------- assemble
def build(args):
    tmpdir = args.tmp or tempfile.mkdtemp(prefix="olidx-")
    os.makedirs(tmpdir, exist_ok=True)
    agg = pass_editions(args.editions, tmpdir)
    wks = pass_works(args.works, tmpdir)
    names = load_authors(args.authors)

    if os.path.exists(args.out):
        os.unlink(args.out)
    db = sqlite3.connect(args.out)
    db.executescript("""
      PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF; PRAGMA page_size=8192;
      CREATE TABLE works(
        id INTEGER PRIMARY KEY, key TEXT UNIQUE, title TEXT, author TEXT,
        year INT, cover INT, ocaid TEXT, gut TEXT, ecount INT,
        mask_lo INT, mask_hi INT);  -- subject bitmask split for 32-bit JS bit ops
      CREATE TABLE subject_rank(si INT, rank INT, wid INT, PRIMARY KEY(si, rank)) WITHOUT ROWID;
      CREATE TABLE meta(k TEXT PRIMARY KEY, v TEXT);
    """)
    heaps = [[] for _ in SUBJECTS]
    cap = args.cap
    kept = 0
    wid = 0
    batch = []

    def merge_rows():
        """Two-pointer merge of sorted agg.tsv and wk.sorted.tsv on work key."""
        fa = open(agg, encoding="utf-8")
        fw = open(wks, encoding="utf-8")
        la, lw = fa.readline(), fw.readline()
        while lw:
            pw = lw.rstrip("\n").split("\t")
            if len(pw) != 5:
                lw = fw.readline(); continue
            wkey = pw[0]
            ec, yr, oc, gut, ecov = 1, 0, "", "", 0
            while la:
                pa = la.rstrip("\n").split("\t")
                ak = pa[0]
                if ak < wkey:
                    la = fa.readline(); continue
                if ak == wkey and len(pa) == 6:
                    ec, yr, oc, gut, ecov = int(pa[1]), int(pa[2]), pa[3], pa[4], int(pa[5])
                    la = fa.readline()
                break
            yield wkey, pw[1], pw[2], int(pw[3] or 0), int(pw[4] or 0), ec, yr, oc, gut, ecov
            lw = fw.readline()
        fa.close(); fw.close()

    for wkey, title, akey, wcov, mask, ec, yr, oc, gut, ecov in merge_rows():
        cover = wcov or ecov
        if not (cover or oc):
            continue  # nothing to shelve or read — skip thin records
        author = names.get(akey, "Unknown")
        wid += 1
        batch.append((wid, wkey, title, author, yr or None, cover or None,
                      oc or None, gut or None, ec,
                      mask & 0xFFFFFFFF, (mask >> 32) & 0xFFFFFFFF))
        if mask:
            for i in range(64):
                if mask & (1 << i):
                    item = (ec, wkey, wid)  # min-heap keeps the top-`cap` by ecount
                    if len(heaps[i]) < cap:
                        heapq.heappush(heaps[i], item)
                    elif item > heaps[i][0]:
                        heapq.heapreplace(heaps[i], item)
        kept += 1
        if len(batch) >= 20000:
            db.executemany("INSERT INTO works VALUES(?,?,?,?,?,?,?,?,?,?,?)", batch)
            batch = []
            if kept % 1000000 < 20000:
                log("kept rows: ~%dM" % (kept / 1000000))
    if batch:
        db.executemany("INSERT INTO works VALUES(?,?,?,?,?,?,?,?,?,?,?)", batch)
    db.commit()
    log("works kept: %d — writing subject ranks" % kept)

    for i, h in enumerate(heaps):
        # rank 0 = most editions; ties broken by key for determinism
        ordered = sorted(h, key=lambda t: (-t[0], t[1]))
        db.executemany("INSERT INTO subject_rank VALUES(?,?,?)",
                       [(i, r, t[2]) for r, t in enumerate(ordered)])
    db.commit()
    log("building FTS")
    db.executescript("""
      CREATE VIRTUAL TABLE fts USING fts5(title, author, content='works', content_rowid='id');
      INSERT INTO fts(rowid, title, author) SELECT id, title, author FROM works;
    """)
    counts = {SUBJECTS[i]: len(heaps[i]) for i in range(64)}
    db.execute("INSERT INTO meta VALUES('built',?)", (time.strftime("%Y-%m-%d"),))
    db.execute("INSERT INTO meta VALUES('works',?)", (str(kept),))
    db.execute("INSERT INTO meta VALUES('subjects',?)", (json.dumps(SUBJECTS),))
    db.execute("INSERT INTO meta VALUES('subject_counts',?)", (json.dumps(counts),))
    db.execute("INSERT INTO meta VALUES('cap',?)", (str(cap),))
    db.commit()
    log("vacuum")
    db.execute("VACUUM")
    db.close()
    log("done: %s (%.2f GB)" % (args.out, os.path.getsize(args.out) / 1e9))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--editions", required=True)
    ap.add_argument("--works", required=True)
    ap.add_argument("--authors", required=True)
    ap.add_argument("--out", default="catalog.db")
    ap.add_argument("--cap", type=int, default=50000)
    ap.add_argument("--tmp", default=None)
    build(ap.parse_args())
