#!/usr/bin/env python3
"""Build a LARGE law pre-cache from CourtListener BULK DATA — no API, no rate limit.

The CourtListener REST API now throttles free tokens to 125 requests/day, far too
little to pre-cache thousands of opinion texts. But Free Law Project also publishes
the entire corpus as public-domain bulk CSV files on S3, with no rate limit at all.
This script streams those files and fills the `bodies` table (opinion_id -> text)
that the server reads for instant, zero-API opinion display.

It joins two bulk files:
  * opinion-clusters-YYYY-MM-DD.csv.bz2  — metadata incl. `citation_count` (how famous)
  * opinions-YYYY-MM-DD.csv.bz2          — the actual text, keyed by opinion `id`,
                                           linked to a cluster via `cluster_id`

Strategy (two streaming passes, neither lands the whole uncompressed file on disk):
  pass 1: read clusters, keep the most-cited cluster_ids (TARGET_CLUSTERS of them)
  pass 2: read opinions, for any opinion in a target cluster, store its cleaned text
          under its opinion id — the SAME id the live search returns, so it hits.

Because the bulk files are snapshots regenerated quarterly, the cache is current as
of the last quarter — fine for the famous, long-settled opinions people search for.

Env:
  CLUSTERS_URL    full https URL to the opinion-clusters .csv.bz2 (required unless
                  AUTO_DISCOVER=1, which finds the newest pair from the S3 listing)
  OPINIONS_URL    full https URL to the opinions .csv.bz2 (same note)
  AUTO_DISCOVER   "1" to fetch the S3 bucket listing and pick the newest dated pair
  OUT             sqlite path to write/extend (default law.db) — extends the SAME
                  db the wing builder makes, so works/meta stay intact
  TARGET_CLUSTERS how many of the most-cited clusters to keep texts for (default 50000;
                  ~1.5GB of opinion text — a strong default, fine on a Railway Pro 1TB tier.
                  Lower it (e.g. 8000) for a leaner cache, or raise it for fuller coverage.)
  MIN_CITES       skip clusters cited fewer than this many times (default 1)
  MAX_BODY_CHARS  truncate any single opinion text to this many chars (default 1_200_000)
  PROGRESS_EVERY  log every N rows scanned (default 200000)

Run where it can reach the internet (your own machine / a CI job) — NOT inside the
sandbox, which can't reach S3. Then commit/upload the resulting law.db like usual.
"""
import bz2
import csv
import io
import os
import sqlite3
import sys
import time
import urllib.request

S3_LIST = ("https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/"
           "?list-type=2&prefix=bulk-data/")
UA = {"User-Agent": "HexadecagonLibrary/1.0 (+stacks-catalog bulk precache)"}

OUT = os.environ.get("OUT", "law.db")
TARGET_CLUSTERS = int(os.environ.get("TARGET_CLUSTERS", "50000"))
MIN_CITES = int(os.environ.get("MIN_CITES", "1"))
MAX_BODY_CHARS = int(os.environ.get("MAX_BODY_CHARS", "1200000"))
PROGRESS_EVERY = int(os.environ.get("PROGRESS_EVERY", "200000"))

# CSV from Postgres COPY can carry very large text fields; raise the limit.
csv.field_size_limit(min(sys.maxsize, 2**31 - 1))


def log(msg):
    print(msg, flush=True)


def discover_urls():
    """Find the newest opinion-clusters + opinions .bz2 pair from the S3 listing."""
    log("discovering newest bulk files from S3 listing\u2026")
    req = urllib.request.Request(S3_LIST, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        xml = r.read().decode("utf-8", "replace")
    keys = []
    for m in xml.split("<Key>")[1:]:
        keys.append(m.split("</Key>")[0])
    base = "https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/"

    def newest(prefix):
        cand = sorted(k for k in keys
                      if k.startswith("bulk-data/" + prefix) and k.endswith(".csv.bz2"))
        return base + cand[-1] if cand else None

    cu = newest("opinion-clusters-")
    ou = newest("opinions-")
    return cu, ou


def open_bz2_stream(url):
    """Yield decoded text lines from a remote .bz2 CSV without saving it whole."""
    req = urllib.request.Request(url, headers=UA)
    resp = urllib.request.urlopen(req, timeout=120)
    decomp = bz2.BZ2Decompressor()
    pending = b""

    def chunks():
        nonlocal pending
        while True:
            raw = resp.read(1 << 20)  # 1 MiB network chunks
            if not raw:
                break
            try:
                out = decomp.decompress(raw)
            except OSError as e:  # corrupt stream
                raise RuntimeError("bz2 decompress failed: %s" % e)
            if out:
                yield out
        # flush any buffered tail
        tail = decomp.flush() if hasattr(decomp, "flush") else b""
        if tail:
            yield tail

    # Wrap the byte chunks in a text stream so csv can read it line by line
    byte_iter = chunks()

    class _Reader(io.RawIOBase):
        def readable(self):
            return True

        def readinto(self, b):
            nonlocal pending
            while len(pending) < len(b):
                try:
                    pending += next(byte_iter)
                except StopIteration:
                    break
            if not pending:
                return 0
            n = min(len(b), len(pending))
            b[:n] = pending[:n]
            pending = pending[n:]
            return n

    return resp, io.TextIOWrapper(io.BufferedReader(_Reader()), encoding="utf-8",
                                  errors="replace", newline="")


def csv_rows(text_stream):
    """CSV reader matching the bulk export dialect (FORCE_QUOTE *, ESCAPE '\\')."""
    return csv.reader(text_stream, quotechar='"', escapechar="\\",
                      doublequote=False, strict=False)


def index_of(header, name):
    try:
        return header.index(name)
    except ValueError:
        return -1


def pass1_clusters(url):
    """Return a dict cluster_id -> (citation_count, case_name) for the top cited."""
    log("pass 1: streaming opinion clusters from %s" % url)
    resp, ts = open_bz2_stream(url)
    try:
        rdr = csv_rows(ts)
        header = next(rdr)
        ci_id = index_of(header, "id")
        ci_cc = index_of(header, "citation_count")
        ci_nm = index_of(header, "case_name")
        if ci_id < 0 or ci_cc < 0:
            raise RuntimeError("clusters CSV missing id/citation_count columns: %r" % header)
        # keep a running top list; for memory we collect (cites, id, name) then trim
        kept = []
        n = 0
        for row in rdr:
            n += 1
            if n % PROGRESS_EVERY == 0:
                log("  clusters scanned: %d (holding %d)" % (n, len(kept)))
            try:
                cc = int(row[ci_cc] or 0)
            except (ValueError, IndexError):
                cc = 0
            if cc < MIN_CITES:
                continue
            cid = row[ci_id] if ci_id < len(row) else ""
            if not cid:
                continue
            name = row[ci_nm] if (0 <= ci_nm < len(row)) else ""
            kept.append((cc, cid, name))
            # periodically trim to keep memory bounded (keep 4x target, then cut)
            if len(kept) > TARGET_CLUSTERS * 4:
                kept.sort(key=lambda t: t[0], reverse=True)
                kept = kept[:TARGET_CLUSTERS * 2]
        kept.sort(key=lambda t: t[0], reverse=True)
        kept = kept[:TARGET_CLUSTERS]
        log("  clusters scanned: %d total; kept top %d (min cites in set: %d)"
            % (n, len(kept), kept[-1][0] if kept else 0))
        return {cid: (cc, name) for (cc, cid, name) in kept}
    finally:
        try:
            resp.close()
        except Exception:
            pass


def clean_text(plain, *htmls):
    """Prefer plain_text; else strip the best HTML variant to readable text."""
    if plain and plain.strip():
        return plain.strip()
    html = ""
    for h in htmls:
        if h and h.strip():
            html = h
            break
    if not html:
        return ""
    import re
    t = re.sub(r"</(p|div|h\d|blockquote|tr|li)>", "\n\n", html, flags=re.I)
    t = re.sub(r"<br\s*/?>", "\n", t, flags=re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = (t.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
         .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return t.strip()


def pass2_opinions(url, target_clusters, con):
    """Stream opinions; store text for any opinion in a target cluster."""
    log("pass 2: streaming opinions from %s" % url)
    resp, ts = open_bz2_stream(url)
    stored = 0
    try:
        rdr = csv_rows(ts)
        header = next(rdr)
        oi_id = index_of(header, "id")
        oi_cl = index_of(header, "cluster_id")
        oi_pt = index_of(header, "plain_text")
        oi_h1 = index_of(header, "html_with_citations")
        oi_h2 = index_of(header, "html")
        oi_h3 = index_of(header, "html_lawbox")
        oi_h4 = index_of(header, "html_columbia")
        if oi_id < 0 or oi_cl < 0:
            raise RuntimeError("opinions CSV missing id/cluster_id columns: %r" % header)
        n = 0
        batch = []
        cur = con.cursor()
        for row in rdr:
            n += 1
            if n % PROGRESS_EVERY == 0:
                log("  opinions scanned: %d (stored %d)" % (n, stored))
            cl = row[oi_cl] if oi_cl < len(row) else ""
            if cl not in target_clusters:
                continue
            oid = row[oi_id] if oi_id < len(row) else ""
            if not oid:
                continue

            def g(i):
                return row[i] if (0 <= i < len(row)) else ""

            txt = clean_text(g(oi_pt), g(oi_h1), g(oi_h2), g(oi_h3), g(oi_h4))
            if not txt or len(txt) < 200:  # skip stubs / empty scans
                continue
            batch.append((str(oid), txt[:MAX_BODY_CHARS]))
            stored += 1
            if len(batch) >= 500:
                cur.executemany("INSERT OR REPLACE INTO bodies VALUES(?,?)", batch)
                con.commit()
                batch = []
        if batch:
            cur.executemany("INSERT OR REPLACE INTO bodies VALUES(?,?)", batch)
            con.commit()
        log("  opinions scanned: %d total; stored %d texts" % (n, stored))
        return stored
    finally:
        try:
            resp.close()
        except Exception:
            pass


def main():
    t0 = time.time()
    clusters_url = os.environ.get("CLUSTERS_URL", "")
    opinions_url = os.environ.get("OPINIONS_URL", "")
    if os.environ.get("AUTO_DISCOVER") == "1" and not (clusters_url and opinions_url):
        cu, ou = discover_urls()
        clusters_url = clusters_url or cu
        opinions_url = opinions_url or ou
        log("discovered:\n  clusters: %s\n  opinions: %s" % (clusters_url, opinions_url))
    if not clusters_url or not opinions_url:
        log("ERROR: set CLUSTERS_URL and OPINIONS_URL (or AUTO_DISCOVER=1).")
        log("Browse the files at: "
            "https://com-courtlistener-storage.s3-us-west-2.amazonaws.com/list.html?prefix=bulk-data/")
        sys.exit(2)

    if not os.path.exists(OUT):
        log("NOTE: %s does not exist yet \u2014 creating it with a bodies table only. "
            "Run the wing builder too so works/meta exist." % OUT)
    con = sqlite3.connect(OUT)
    con.executescript(
        "PRAGMA journal_mode=WAL;"
        "CREATE TABLE IF NOT EXISTS bodies(id TEXT PRIMARY KEY, text TEXT);")

    before = con.execute("SELECT COUNT(*) FROM bodies").fetchone()[0]
    log("bodies table currently holds %d texts" % before)

    targets = pass1_clusters(clusters_url)
    if not targets:
        log("ERROR: no target clusters selected \u2014 check the clusters file/columns.")
        sys.exit(1)
    stored = pass2_opinions(opinions_url, targets, con)

    after = con.execute("SELECT COUNT(*) FROM bodies").fetchone()[0]
    con.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);")
    con.execute("INSERT OR REPLACE INTO meta VALUES('precache_bulk_built', ?)",
                (time.strftime("%Y%m%d-%H%M"),))
    con.execute("INSERT OR REPLACE INTO meta VALUES('precache_bulk_count', ?)",
                (str(after),))
    con.commit()
    con.close()
    log("DONE in %.0fs: bodies %d -> %d (+%d this run, %d opinions matched targets)"
        % (time.time() - t0, before, after, after - before, stored))


if __name__ == "__main__":
    main()
