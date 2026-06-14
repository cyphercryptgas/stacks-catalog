/* ============================================================================
   stacks-catalog server (Railway) — zero dependencies (Node >= 22.5)
   On boot: downloads the SQLite index from this repo's GitHub Release
   (gzipped parts) onto the volume, then serves read-only catalog queries.
   Doc shape matches the Open Stacks front-end exactly.

   Env:
     GITHUB_REPO   e.g. "cyphercryptgas/stacks-catalog"   (required)
     RELEASE_TAG   default "catalog-index"
     DATA_DIR      default "/data"   (mount your Railway volume here)
     PORT          provided by Railway
   ========================================================================= */
"use strict";

const fs = require("fs");
const path = require("path");
const http = require("http");
const https = require("https");
const zlib = require("zlib");
const { DatabaseSync } = require("node:sqlite");

const REPO = process.env.GITHUB_REPO || "";
const TAG = process.env.RELEASE_TAG || "catalog-index";
const DATA_DIR = process.env.DATA_DIR || "/data";
const PORT = process.env.PORT || 3000;
const DB_PATH = path.join(DATA_DIR, "catalog.db");
const VER_PATH = path.join(DATA_DIR, "version.txt");
const ROOM_SIZE = 640;

// ---------------------------------------------------------------- download
function fetchBuf(url, redirects) {
  return new Promise((resolve, reject) => {
    if ((redirects || 0) > 6) return reject(new Error("too many redirects"));
    https.get(url, { headers: { "User-Agent": "stacks-catalog" } }, (res) => {
      if (res.statusCode >= 301 && res.statusCode <= 308 && res.headers.location) {
        res.resume();
        return resolve(fetchBuf(res.headers.location, (redirects || 0) + 1));
      }
      if (res.statusCode !== 200) { res.resume(); return reject(new Error("HTTP " + res.statusCode + " " + url)); }
      const chunks = [];
      res.on("data", (c) => chunks.push(c));
      res.on("end", () => resolve(Buffer.concat(chunks)));
      res.on("error", reject);
    }).on("error", reject);
  });
}

function fetchGunzipAppend(url, dest, redirects) {
  return new Promise((resolve, reject) => {
    if ((redirects || 0) > 6) return reject(new Error("too many redirects"));
    https.get(url, { headers: { "User-Agent": "stacks-catalog" } }, (res) => {
      if (res.statusCode >= 301 && res.statusCode <= 308 && res.headers.location) {
        res.resume();
        return resolve(fetchGunzipAppend(res.headers.location, dest, (redirects || 0) + 1));
      }
      if (res.statusCode !== 200) { res.resume(); return reject(new Error("HTTP " + res.statusCode + " " + url)); }
      const out = fs.createWriteStream(dest, { flags: "a" });
      res.pipe(zlib.createGunzip()).pipe(out);
      out.on("finish", resolve);
      out.on("error", reject);
      res.on("error", reject);
    }).on("error", reject);
  });
}

const assetURL = (name) =>
  "https://github.com/" + REPO + "/releases/download/" + TAG + "/" + name;

async function bootstrap() {
  fs.mkdirSync(DATA_DIR, { recursive: true });
  let meta = null;
  try {
    meta = JSON.parse((await fetchBuf(assetURL("meta.json"))).toString("utf8"));
  } catch (e) {
    console.log("meta.json unreachable (" + e.message + ")");
  }
  const haveDb = fs.existsSync(DB_PATH);
  const haveVer = fs.existsSync(VER_PATH) ? fs.readFileSync(VER_PATH, "utf8").trim() : "";
  if (haveDb && (!meta || meta.version === haveVer)) {
    console.log("index present (version " + (haveVer || "unknown") + ")");
    return;
  }
  if (!meta) throw new Error("no local index and release meta unreachable");
  console.log("downloading index " + meta.version + " (" + meta.parts.length + " parts)");
  const tmp = DB_PATH + ".tmp";
  if (fs.existsSync(tmp)) fs.unlinkSync(tmp);
  for (const part of meta.parts) {
    console.log("  " + part);
    await fetchGunzipAppend(assetURL(part), tmp);
  }
  if (fs.existsSync(DB_PATH)) fs.unlinkSync(DB_PATH);
  fs.renameSync(tmp, DB_PATH);
  fs.writeFileSync(VER_PATH, meta.version);
  console.log("index ready: " + (fs.statSync(DB_PATH).size / 1e9).toFixed(2) + " GB");
}

const WING_NAMES = ["research", "law"];

async function bootstrapWings() {
  let wj = null;
  try {
    wj = JSON.parse((await fetchBuf(assetURL("wings.json"))).toString("utf8"));
  } catch (e) {
    console.log("wings.json unreachable (" + e.message + ") \u2014 wings stay closed");
    return;
  }
  for (const w of WING_NAMES) {
    const m = wj && wj[w];
    if (!m || !m.asset) continue;
    const dbp = path.join(DATA_DIR, w + ".db");
    const vp = path.join(DATA_DIR, w + ".version");
    const have = fs.existsSync(dbp)
      ? (fs.existsSync(vp) ? fs.readFileSync(vp, "utf8").trim() : "") : null;
    if (have !== null && have === m.version) {
      console.log("wing " + w + " present (" + have + ")");
      continue;
    }
    try {
      console.log("downloading wing " + w + " " + m.version);
      const tmp = dbp + ".tmp";
      if (fs.existsSync(tmp)) fs.unlinkSync(tmp);
      await fetchGunzipAppend(assetURL(m.asset), tmp);
      if (fs.existsSync(dbp)) fs.unlinkSync(dbp);
      fs.renameSync(tmp, dbp);
      fs.writeFileSync(vp, m.version);
      console.log("wing " + w + " ready");
    } catch (e) {
      console.log("wing " + w + " download failed: " + e.message);
    }
  }
}

// ---------------------------------------------------------------- serving
function startServer() {
  const db = new DatabaseSync(DB_PATH, { readOnly: true });
  const wings = {};
  for (const w of WING_NAMES) {
    const p = path.join(DATA_DIR, w + ".db");
    if (!fs.existsSync(p)) continue;
    try {
      const wdb = new DatabaseSync(p, { readOnly: true });
      const wm = Object.fromEntries(
        wdb.prepare("SELECT k, v FROM meta").all().map((r) => [r.k, r.v]));
      wings[w] = {
        subjects: JSON.parse(wm.subjects || "[]"),
        built: wm.built || "", version: wm.version || "",
        qRoom: wdb.prepare(
          "SELECT * FROM works WHERE si = ? AND rank >= ? AND rank < ? ORDER BY rank"),
      };
      // pre-cached full texts (opinions), if the builder wrote a bodies table
      try {
        wings[w].qBody = wdb.prepare("SELECT text FROM bodies WHERE id = ?");
      } catch (e) { wings[w].qBody = null; }
      console.log("wing open: " + w + " \u2014 " + wings[w].subjects.length + " halls");
    } catch (e) {
      console.log("wing " + w + " failed to open: " + e.message);
    }
  }
  const metaRows = Object.fromEntries(
    db.prepare("SELECT k, v FROM meta").all().map((r) => [r.k, r.v]));
  const SUBJECTS = JSON.parse(metaRows.subjects || "[]");
  const subjectCounts = JSON.parse(metaRows.subject_counts || "{}");

  const qRoom = db.prepare(
    "SELECT w.* FROM subject_rank sr JOIN works w ON w.id = sr.wid " +
    "WHERE sr.si = ? AND sr.rank >= ? AND sr.rank < ? ORDER BY sr.rank");
  const qWork = db.prepare("SELECT * FROM works WHERE key = ?");
  const qFts = db.prepare(
    "SELECT w.* FROM works w WHERE w.id IN " +
    "(SELECT rowid FROM fts WHERE fts MATCH ? ORDER BY rank LIMIT 600) " +
    "ORDER BY w.ecount DESC LIMIT ?");
  const qLike = db.prepare(
    "SELECT * FROM works WHERE title LIKE ? ORDER BY ecount DESC LIMIT ?");
  const qCount = db.prepare("SELECT COUNT(*) AS n FROM subject_rank WHERE si = ?");
  const qAt = db.prepare(
    "SELECT w.* FROM subject_rank sr JOIN works w ON w.id = sr.wid WHERE sr.si = ? AND sr.rank = ?");
  const nonEmptySis = [];
  for (let i = 0; i < SUBJECTS.length; i++) {
    if (qCount.get(i).n > 0) nonEmptySis.push(i);
  }

  const subjectsOf = (lo, hi) => {
    const out = [];
    for (let i = 0; i < SUBJECTS.length && out.length < 6; i++) {
      const bit = i < 32 ? (lo >>> i) & 1 : (hi >>> (i - 32)) & 1;
      if (bit) out.push(SUBJECTS[i]);
    }
    return out;
  };
  const doc = (r) => r && {
    key: r.key,
    title: r.title,
    author: r.author || "Unknown",
    year: r.year || null,
    cover: r.cover || null,
    access: r.ocaid ? (r.year && r.year < 1930 ? "public" : "borrowable") : "no_ebook",
    ia: r.ocaid || null,
    gutenberg: r.gut || null,
    editions: r.ecount || 1,
    subjects: subjectsOf(Number(r.mask_lo) || 0, Number(r.mask_hi) || 0),
  };

  function send(res, code, obj, noCache) {
    res.writeHead(code, {
      "Content-Type": "application/json; charset=utf-8",
      "Access-Control-Allow-Origin": "*",
      "Cache-Control": noCache ? "no-store" : "public, max-age=86400",
    });
    res.end(JSON.stringify(obj));
  }

  // ---- Plateau 2: Project Gutenberg full-text proxy ----
  // Fetches the plaintext once, strips PG boilerplate, caches to disk forever.
  const GUT_BASE = (process.env.GUTENBERG_BASE || "https://www.gutenberg.org").replace(/\/+$/, "");
  const TEXTS_DIR = path.join(DATA_DIR, "texts");
  const GUT_MAX = 10 * 1024 * 1024; // 10 MB of plain text is beyond any single book
  const gutInflight = new Map();

  function stripPG(t) {
    t = t.replace(/^\uFEFF/, "").replace(/\r\n?/g, "\n");
    const sm = t.match(/\*\*\*\s*START OF (?:THE|THIS) PROJECT GUTENBERG EBOOK[^\n]*/i);
    if (sm) t = t.slice(sm.index + sm[0].length);
    const em = t.match(/\*\*\*\s*END OF (?:THE|THIS) PROJECT GUTENBERG EBOOK/i);
    if (em) t = t.slice(0, em.index);
    return t.trim() + "\n";
  }

  async function fetchGut(id) {
    // Prefer the HTML edition (it carries the book's actual illustrations);
    // fall back to plaintext, which gets its boilerplate stripped.
    const tries = [
      { kind: "html", url: GUT_BASE + "/cache/epub/" + id + "/pg" + id + "-images.html" },
      { kind: "html", url: GUT_BASE + "/cache/epub/" + id + "/pg" + id + ".html" },
      { kind: "text", url: GUT_BASE + "/cache/epub/" + id + "/pg" + id + ".txt" },
      { kind: "text", url: GUT_BASE + "/files/" + id + "/" + id + "-0.txt" },
      { kind: "text", url: GUT_BASE + "/files/" + id + "/" + id + ".txt" },
    ];
    let lastErr = "unreachable";
    for (const t of tries) {
      const ctl = new AbortController();
      const tm = setTimeout(() => ctl.abort(), 30000);
      try {
        const r = await fetch(t.url, {
          signal: ctl.signal,
          headers: { "User-Agent": "HexadecagonLibrary/1.0 (+https://github.com/cyphercryptgas/stacks-catalog)" },
        });
        if (!r.ok) { lastErr = "HTTP " + r.status + " " + t.url; continue; }
        const buf = Buffer.from(await r.arrayBuffer());
        if (buf.length > GUT_MAX) { lastErr = "text too large"; continue; }
        const body = t.kind === "text" ? stripPG(buf.toString("utf8")) : buf.toString("utf8");
        if (body.length < 200) { lastErr = "empty after trimming"; continue; }
        return { kind: t.kind, body };
      } catch (e) {
        lastErr = e && e.name === "AbortError" ? "timeout" : (e.message || "fetch failed");
      } finally { clearTimeout(tm); }
    }
    throw new Error(lastErr);
  }

  function gutenbergRoute(id, res) {
    const fHtml = path.join(TEXTS_DIR, id + ".html");
    const fText = path.join(TEXTS_DIR, id + ".txt");
    const serve = (kind, body) => {
      res.writeHead(200, {
        "Content-Type": (kind === "html" ? "text/html" : "text/plain") + "; charset=utf-8",
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=31536000, immutable",
      });
      res.end(body);
    };
    if (fs.existsSync(fHtml)) return serve("html", fs.readFileSync(fHtml));
    if (fs.existsSync(fText)) return serve("text", fs.readFileSync(fText));
    let p = gutInflight.get(id);
    if (!p) {
      p = fetchGut(id).then((got) => {
        fs.mkdirSync(TEXTS_DIR, { recursive: true });
        const file = got.kind === "html" ? fHtml : fText;
        fs.writeFileSync(file + ".tmp." + process.pid, got.body);
        fs.renameSync(file + ".tmp." + process.pid, file);
        console.log("gutenberg " + id + " cached as " + got.kind + " (" + (got.body.length / 1024).toFixed(0) + " KB)");
        return got;
      }).finally(() => gutInflight.delete(id));
      gutInflight.set(id, p);
    }
    p.then((got) => serve(got.kind, got.body))
      .catch((e) => send(res, 502, { error: "gutenberg fetch failed: " + e.message }, true));
  }

  // ---- Plateau 3: LibriVox audiobooks + Project Gutenberg catalog cross-reference ----
  const LV_BASE = (process.env.LIBRIVOX_BASE || "https://librivox.org").replace(/\/+$/, "");
  const AUDIO_DIR = path.join(DATA_DIR, "audio");
  const UA = { "User-Agent": "HexadecagonLibrary/1.0 (+https://github.com/cyphercryptgas/stacks-catalog)" };

  const normTitle = (s) => String(s || "").toLowerCase()
    .replace(/[^a-z0-9 ]+/g, " ")
    .replace(/\b(the|a|an|or|and)\b/g, " ")
    .replace(/\s+/g, " ").trim();
  const surname = (a) => {
    const w = String(a || "").trim().split(/\s+/);
    return w.length ? w[w.length - 1].toLowerCase().replace(/[^a-z]/g, "") : "";
  };
  const slugOf = (title, author) =>
    (normTitle(title) + "-" + surname(author)).replace(/[^a-z0-9-]+/g, "-").slice(0, 80);

  async function jfetch(url, ms) {
    const ctl = new AbortController();
    const tm = setTimeout(() => ctl.abort(), ms || 20000);
    try {
      const r = await fetch(url, { signal: ctl.signal, headers: UA });
      if (!r.ok) throw new Error("HTTP " + r.status + " " + url);
      return await r.json();
    } finally { clearTimeout(tm); }
  }

  async function lvLookup(title, author) {
    const j = await jfetch(LV_BASE + "/api/feed/audiobooks/?format=json&extended=1&limit=25&title=" +
      encodeURIComponent(title), 25000);
    let books = (j && j.books) || [];
    if (!Array.isArray(books)) books = [];
    const nt = normTitle(title), sn = surname(author);
    const scored = [];
    for (const b of books) {
      if (b.language && !/english/i.test(b.language)) continue;
      const bt = normTitle(b.title);
      let score = bt === nt ? 3 : (bt.startsWith(nt) || nt.startsWith(bt)) ? 2 :
        (bt.includes(nt) || nt.includes(bt)) ? 1 : 0;
      if (!score) continue;
      if (sn) {
        const auths = (b.authors || [])
          .map((a) => ((a.last_name || "") + " " + (a.first_name || "")).toLowerCase()).join(" | ");
        if (!auths.includes(sn)) continue; // wrong author entirely — skip
        score += 2;
      }
      scored.push({ b, score });
    }
    scored.sort((x, y) => y.score - x.score ||
      (Number(y.b.num_sections) || 0) - (Number(x.b.num_sections) || 0));
    if (!scored.length) return { found: false };
    const b = scored[0].b;
    let secs = Array.isArray(b.sections) ? b.sections : [];
    if (!secs.length && b.id) { // some feeds omit sections; the audiotracks feed has them
      try {
        const t = await jfetch(LV_BASE + "/api/feed/audiotracks/?format=json&project_id=" + b.id, 20000);
        secs = (t && (t.sections || t.audiotracks)) || [];
      } catch (e) {}
    }
    const sections = secs.filter((s) => s && s.listen_url).slice(0, 300).map((s, i) => ({
      n: Number(s.section_number) || i + 1,
      title: s.title || ("Section " + (i + 1)),
      url: s.listen_url,
      playtime: s.playtime || null,
    }));
    return {
      found: true, id: b.id, title: b.title,
      author: (b.authors && b.authors[0])
        ? ((b.authors[0].first_name || "") + " " + (b.authors[0].last_name || "")).trim() : null,
      totaltime: b.totaltime || null,
      url: b.url_librivox || null,
      sections,
    };
  }

  const lvInflight = new Map();
  function audioRoute(q, res) {
    const title = String(q.searchParams.get("title") || "").slice(0, 200).trim();
    const author = String(q.searchParams.get("author") || "").slice(0, 120).trim();
    if (title.length < 2) return send(res, 400, { error: "title required" }, true);
    const slug = slugOf(title, author);
    const file = path.join(AUDIO_DIR, "lv-" + slug + ".json");
    if (fs.existsSync(file)) {
      try { return send(res, 200, JSON.parse(fs.readFileSync(file, "utf8"))); } catch (e) {}
    }
    let p = lvInflight.get(slug);
    if (!p) {
      p = lvLookup(title, author).then((out) => {
        fs.mkdirSync(AUDIO_DIR, { recursive: true });
        fs.writeFileSync(file, JSON.stringify(out));
        if (out.found) console.log("librivox: \"" + title + "\" -> #" + out.id +
          " (" + out.sections.length + " sections)");
        return out;
      }).finally(() => lvInflight.delete(slug));
      lvInflight.set(slug, p);
    }
    p.then((out) => send(res, 200, out))
      .catch((e) => send(res, 502, { error: "librivox lookup failed: " + e.message }, true));
  }

  // Project Gutenberg's own catalog (CSV, ~75k rows) — loaded lazily, kept in memory.
  // Fills the gaps where Open Library's data lacks the gutenberg link.
  let pgCatalog = null, pgLoading = null;
  function parseCSV(text) {
    const rows = []; let row = [], field = "", inQ = false;
    for (let i = 0; i < text.length; i++) {
      const c = text[i];
      if (inQ) {
        if (c === '"') { if (text[i + 1] === '"') { field += '"'; i++; } else inQ = false; }
        else field += c;
      } else if (c === '"') inQ = true;
      else if (c === ",") { row.push(field); field = ""; }
      else if (c === "\n") { row.push(field.replace(/\r$/, "")); rows.push(row); row = []; field = ""; }
      else field += c;
    }
    if (field !== "" || row.length) { row.push(field.replace(/\r$/, "")); rows.push(row); }
    return rows;
  }
  function loadPgCatalog() {
    if (pgCatalog) return Promise.resolve(pgCatalog);
    if (!pgLoading) {
      pgLoading = (async () => {
        const ctl = new AbortController();
        const tm = setTimeout(() => ctl.abort(), 60000);
        try {
          const r = await fetch(GUT_BASE + "/cache/epub/feeds/pg_catalog.csv",
            { signal: ctl.signal, headers: UA });
          if (!r.ok) throw new Error("HTTP " + r.status);
          const rows = parseCSV(await r.text());
          const head = rows[0].map((s) => s.toLowerCase());
          const iId = head.indexOf("text#"), iType = head.indexOf("type"),
            iTitle = head.indexOf("title"), iLang = head.indexOf("language"),
            iAuth = head.indexOf("authors");
          const list = [];
          for (let i = 1; i < rows.length; i++) {
            const r2 = rows[i];
            if ((r2[iType] || "") !== "Text") continue;
            if (!/(^|;)\s*en\b/.test(r2[iLang] || "")) continue;
            list.push({ id: Number(r2[iId]), nt: normTitle(r2[iTitle]), au: (r2[iAuth] || "").toLowerCase() });
          }
          pgCatalog = list;
          console.log("pg catalog loaded: " + list.length + " english texts");
          return list;
        } finally { clearTimeout(tm); pgLoading = null; }
      })();
    }
    return pgLoading;
  }
  async function pgFind(title, author) {
    const list = await loadPgCatalog();
    const nt = normTitle(title), sn = surname(author);
    if (!nt) return { gutenberg: null };
    const ok = (e) => !sn || e.au.includes(sn);
    let hits = list.filter((e) => e.nt === nt && ok(e));
    if (!hits.length) hits = list.filter((e) => e.nt &&
      (e.nt.startsWith(nt) || nt.startsWith(e.nt)) && ok(e));
    if (!hits.length) return { gutenberg: null };
    hits.sort((a, b) => a.id - b.id); // earliest PG number is almost always the canonical text
    return { gutenberg: hits[0].id };
  }

  // ---- The Annex: research papers (arXiv) and case law (CourtListener) ----
  const ARXIV_BASE = (process.env.ARXIV_BASE || "http://export.arxiv.org").replace(/\/+$/, "");
  const DOAB_BASE = (process.env.DOAB_BASE || "https://directory.doabooks.org").replace(/\/+$/, "");
  const CL_BASE = (process.env.COURTLISTENER_BASE || "https://www.courtlistener.com").replace(/\/+$/, "");
  const GOVINFO_KEY = process.env.GOVINFO_KEY || "";
  const ANNEX_DIR = path.join(DATA_DIR, "annex");
  const tagOf = (xml, tag) => {
    const m = xml.match(new RegExp("<" + tag + "[^>]*>([\\s\\S]*?)</" + tag + ">"));
    return m ? m[1].replace(/<[^>]+>/g, " ").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
      .replace(/&amp;/g, "&").replace(/&quot;/g, '"').replace(/&#39;/g, "'")
      .replace(/\s+/g, " ").trim() : "";
  };
  const CL_TOKEN = process.env.CL_TOKEN || "";
  async function tfetch(url, ms) {
    const ctl = new AbortController();
    const tm = setTimeout(() => ctl.abort(), ms || 20000);
    try {
      const headers = Object.assign({}, UA);
      // CourtListener increasingly requires auth even for reads; send the token.
      if (CL_TOKEN && url.indexOf("courtlistener.com") !== -1) {
        headers.Authorization = "Token " + CL_TOKEN;
      }
      const r = await fetch(url, { signal: ctl.signal, headers });
      if (!r.ok) throw new Error("HTTP " + r.status + " " + url);
      return await r.text();
    } finally { clearTimeout(tm); }
  }
  async function tfetchPost(url, body, ms) {
    const ctl = new AbortController();
    const tm = setTimeout(() => ctl.abort(), ms || 20000);
    try {
      const r = await fetch(url, {
        method: "POST", signal: ctl.signal,
        headers: Object.assign({ "Content-Type": "application/json" }, UA),
        body,
      });
      if (!r.ok) throw new Error("HTTP " + r.status + " " + url);
      return await r.text();
    } finally { clearTimeout(tm); }
  }
  function annexCache(file) {
    if (fs.existsSync(file)) {
      try { return JSON.parse(fs.readFileSync(file, "utf8")); } catch (e) {}
    }
    return null;
  }
  function annexSave(file, out) {
    fs.mkdirSync(ANNEX_DIR, { recursive: true });
    fs.writeFileSync(file, JSON.stringify(out));
  }
  async function arxivSearch(qstr) {
    const run = (sq) => tfetch(ARXIV_BASE + "/api/query?search_query=" +
      encodeURIComponent(sq) + "&start=0&max_results=20&sortBy=relevance", 20000);
    let xml = await run('all:"' + qstr + '"');
    if (xml.indexOf("<entry>") === -1) { // the exact phrase missed — loosen to AND terms
      const terms = qstr.split(/\s+/).filter(Boolean).slice(0, 6);
      if (terms.length) xml = await run(terms.map((t) => "all:" + t).join(" AND "));
    }
    const docs = [];
    const entries = xml.split("<entry>").slice(1);
    for (const e of entries) {
      const id = tagOf(e, "id"); // e.g. http://arxiv.org/abs/2401.12345v2
      const aid = (id.match(/abs\/([^\s]+?)(v\d+)?$/) || [])[1] || id;
      const authors = [...e.matchAll(/<name>([\s\S]*?)<\/name>/g)].map((m) => m[1].trim());
      const pdfm = e.match(/<link[^>]*title="pdf"[^>]*href="([^"]+)"/) ||
        e.match(/<link[^>]*href="([^"]+\/pdf\/[^"]+)"/);
      docs.push({
        id: aid,
        title: tagOf(e, "title"),
        authors: authors.slice(0, 4),
        year: (tagOf(e, "published").match(/^(\d{4})/) || [])[1] || null,
        summary: tagOf(e, "summary").slice(0, 420),
        pdf: pdfm ? pdfm[1].replace(/^http:/, "https:") : null,
        page: id.replace(/^http:/, "https:"),
      });
    }
    return { docs: docs.slice(0, 20) };
  }
  async function doabSearch(qstr) {
    // open-access academic books, full text, from the Directory of Open Access Books
    const url = DOAB_BASE + "/rest/search?query=" + encodeURIComponent(qstr) +
      "&expand=metadata,bitstreams&limit=12&offset=0";
    let items;
    try { items = JSON.parse(await tfetch(url, 16000)); }
    catch (e) { return []; }
    if (!Array.isArray(items)) return [];
    const metaVal = (it, k2) => {
      for (const m of (it.metadata || [])) if (m.key === k2) return m.value;
      return null;
    };
    const metaAll = (it, k2) =>
      (it.metadata || []).filter((m) => m.key === k2).map((m) => m.value);
    const pdfOf = (it) => {
      for (const b of (it.bitstreams || [])) {
        const mime = (b.mimeType || "").toLowerCase();
        const nm = (b.name || "").toLowerCase();
        if (mime.indexOf("pdf") !== -1 || nm.endsWith(".pdf")) {
          const link = b.retrieveLink || b.link;
          if (link) return link.indexOf("http") === 0 ? link : DOAB_BASE + link;
        }
      }
      return null;
    };
    const docs = [];
    for (const it of items.slice(0, 12)) {
      const handle = it.handle || it.uuid || "";
      docs.push({
        kind: "book",
        id: "doab:" + handle,
        title: (metaVal(it, "dc.title") || "Untitled").slice(0, 220),
        authors: metaAll(it, "dc.contributor.author").filter(Boolean).slice(0, 4),
        year: ((metaVal(it, "dc.date.issued") || "").match(/(\d{4})/) || [])[1] || null,
        summary: (metaVal(it, "dc.description.abstract") || "").slice(0, 420),
        pdf: pdfOf(it),
        page: handle ? "https://directory.doabooks.org/handle/" + handle : null,
      });
    }
    return docs;
  }
  async function researchSearch(qstr) {
    // run arXiv and DOAB together; papers first, then open-access books
    const [papers, books] = await Promise.all([
      arxivSearch(qstr).then((r) => (r && r.docs) || []).catch(() => []),
      doabSearch(qstr).catch(() => []),
    ]);
    const tagged = papers.map((p) => Object.assign({ kind: "paper" }, p));
    return { docs: tagged.concat(books).slice(0, 28) };
  }
  async function lawSearch(qstr) {
    const docs = [];
    // 1) opinions (case law)
    try {
      const j = JSON.parse(await tfetch(CL_BASE + "/api/rest/v4/search/?type=o&order_by=score%20desc&q=" +
        encodeURIComponent(qstr), 18000));
      for (const r of (j.results || []).slice(0, 12)) {
        const op = (r.opinions && r.opinions[0]) || {};
        docs.push({
          kind: "opinion",
          id: op.id || null,
          title: r.caseName || r.case_name || "Untitled case",
          court: r.court_citation_string || r.court || "",
          year: ((r.dateFiled || r.date_filed || "").match(/^(\d{4})/) || [])[1] || null,
          cite: Array.isArray(r.citation) ? r.citation.slice(0, 2).join(" \u00b7 ") : (r.citation || ""),
          url: r.absolute_url ? CL_BASE + r.absolute_url : null,
        });
      }
    } catch (e) { /* opinions optional */ }
    // 2) dockets (the lawsuits themselves)
    try {
      const j = JSON.parse(await tfetch(CL_BASE + "/api/rest/v4/search/?type=r&order_by=score%20desc&q=" +
        encodeURIComponent(qstr), 18000));
      for (const r of (j.results || []).slice(0, 8)) {
        const did = r.docket_id || r.id;
        if (!did) continue;
        docs.push({
          kind: "docket",
          id: "recap:" + did,
          title: r.caseName || r.case_name || "Untitled docket",
          court: (r.court || r.court_id || "") +
            (r.docketNumber || r.docket_number ? " \u00b7 " + (r.docketNumber || r.docket_number) : ""),
          year: ((r.dateFiled || r.date_filed || "").match(/^(\d{4})/) || [])[1] || null,
          cite: "",
          url: r.docket_absolute_url ? CL_BASE + r.docket_absolute_url : null,
        });
      }
    } catch (e) { /* dockets optional */ }
    // 3) statutes & regulations (govinfo full-text search across USCODE/CFR/PLAW)
    try {
      if (GOVINFO_KEY) {
        // collection filter MUST be a field operator inside the query string;
        // a separate "collections" field is ignored and returns every collection.
        const body = JSON.stringify({
          query: "collection:(USCODE OR CFR OR PLAW) AND (" + qstr + ")",
          pageSize: "8", offsetMark: "*",
          sorts: [{ field: "score", sortOrder: "DESC" }],
        });
        const sj = JSON.parse(await tfetchPost(
          "https://api.govinfo.gov/search?api_key=" + encodeURIComponent(GOVINFO_KEY),
          body, 16000));
        for (const r of (sj.results || []).slice(0, 8)) {
          const pid = r.packageId || r.granuleId || "";
          const gid = r.granuleId || "";
          // derive collection from the id prefix so the label is always honest
          const prefix = (pid.split("-")[0] || "").toUpperCase();
          // only keep the three statute collections; skip anything else that slips through
          if (["USCODE", "CFR", "PLAW"].indexOf(prefix) === -1) continue;
          const pkgId = r.packageId || (gid ? gid.split("-sec")[0].split("-vol")[0] : pid);
          const detailUrl = gid && r.packageId
            ? "https://www.govinfo.gov/app/details/" + r.packageId + "/" + gid
            : "https://www.govinfo.gov/app/details/" + (pkgId || pid);
          docs.push({
            kind: "statute",
            id: gid || pid,
            title: (r.title || pid).slice(0, 200),
            court: prefix === "CFR" ? "Code of Federal Regulations"
              : prefix === "PLAW" ? "Public Law" : "United States Code",
            year: (r.dateIssued || "").slice(0, 4) || null,
            cite: "",
            url: detailUrl,
            pdf: null,
          });
        }
      }
    } catch (e) { /* statutes optional */ }
    return { docs: docs.slice(0, 28) };
  }
  async function clOpinionText(j) {
    let text = j.plain_text || "";
    if (!text) {
      const html = j.html_with_citations || j.html || j.html_lawbox || j.html_columbia || "";
      text = html.replace(/<\/(p|div|h\d|blockquote)>/gi, "\n\n")
        .replace(/<br\s*\/?>/gi, "\n").replace(/<[^>]+>/g, " ")
        .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
        .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/[ \t]+/g, " ");
    }
    return text.trim();
  }
  async function lawCase(id) {
    // 1) pre-cached in the law wing DB? serve instantly, no live call.
    try {
      const lw = wings.law;
      if (lw && lw.qBody) {
        const hit = lw.qBody.get(String(id));
        if (hit && hit.text) return { id, text: hit.text, cached: true };
      }
    } catch (e) { /* fall through to live fetch */ }
    // 2) live fetch (opinion id, then cluster fallback)
    let text = "";
    try {
      const j = JSON.parse(await tfetch(CL_BASE + "/api/rest/v4/opinions/" + id + "/?format=json", 20000));
      text = await clOpinionText(j);
    } catch (e1) {
      if (String(e1.message).indexOf("HTTP 429") !== -1) {
        const err = new Error("RATELIMIT");
        err.rate = true;
        throw err;
      }
      try {
        const c = JSON.parse(await tfetch(CL_BASE + "/api/rest/v4/clusters/" + id + "/?format=json", 18000));
        const subs = c.sub_opinions || [];
        const opId = String(subs[0] || "").match(/opinions\/(\d+)\//);
        if (opId) {
          const j2 = JSON.parse(await tfetch(CL_BASE + "/api/rest/v4/opinions/" + opId[1] + "/?format=json", 18000));
          text = await clOpinionText(j2);
        }
      } catch (e2) {
        if (String(e2.message).indexOf("HTTP 429") !== -1) {
          const err = new Error("RATELIMIT"); err.rate = true; throw err;
        }
        throw new Error("opinion " + id + " unavailable (" + (e1.message || e2.message) + ")");
      }
    }
    return { id, text: (text || "").slice(0, 1200000) };
  }
  async function lawDocket(id) {
    const dj = JSON.parse(await tfetch(CL_BASE + "/api/rest/v4/dockets/" + id + "/?format=json", 20000));
    const caseName = dj.case_name || dj.case_name_full || "Untitled docket";
    const lines = [];
    lines.push(caseName);
    lines.push("");
    const court = dj.court_id || (dj.court || "");
    if (court) lines.push("Court: " + court);
    if (dj.docket_number) lines.push("Docket No. " + dj.docket_number);
    if (dj.date_filed) lines.push("Filed: " + dj.date_filed);
    if (dj.date_terminated) lines.push("Terminated: " + dj.date_terminated);
    if (dj.assigned_to_str) lines.push("Assigned to: " + dj.assigned_to_str);
    if (dj.nature_of_suit) lines.push("Nature of suit: " + dj.nature_of_suit);
    if (dj.cause) lines.push("Cause: " + dj.cause);
    lines.push("");
    // parties (best-effort; may be a separate endpoint)
    try {
      const pj = JSON.parse(await tfetch(
        CL_BASE + "/api/rest/v4/parties/?docket=" + id + "&format=json", 18000));
      const parties = (pj.results || []).slice(0, 24)
        .map((p) => p.name + (p.party_types && p.party_types[0] ?
          " (" + p.party_types[0].name + ")" : "")).filter(Boolean);
      if (parties.length) {
        lines.push("PARTIES");
        for (const p of parties) lines.push("  " + p);
        lines.push("");
      }
    } catch (e) { /* parties optional */ }
    // docket entries: the filing history
    let entries = [];
    try {
      let url = CL_BASE + "/api/rest/v4/docket-entries/?docket=" + id +
        "&order_by=entry_number&format=json";
      let guard = 0;
      while (url && entries.length < 400 && guard++ < 5) {
        const ej = JSON.parse(await tfetch(url, 20000));
        for (const e of (ej.results || [])) {
          const num = e.entry_number != null ? "#" + e.entry_number + "  " : "";
          const date = e.date_filed ? e.date_filed + " \u2014 " : "";
          const desc = (e.description || "").replace(/\s+/g, " ").trim();
          const docs = (e.recap_documents || []).filter((d) => d.filepath_local || d.is_available);
          const tag = docs.length ? "  [" + docs.length + " document" +
            (docs.length === 1 ? "" : "s") + " freed]" : "";
          entries.push(num + date + (desc || "(no description)") + tag);
        }
        url = ej.next;
      }
    } catch (e) { /* entries optional */ }
    if (entries.length) {
      lines.push("DOCKET (" + entries.length + " entries shown)");
      lines.push("");
      for (const e of entries) lines.push(e);
    } else {
      lines.push("No docket entries have been freed from PACER for this case yet.");
      lines.push("The case record exists, but its filings are still behind the PACER paywall");
      lines.push("until someone with the RECAP extension pulls them.");
    }
    return { id, text: lines.join("\n").slice(0, 1200000), caseName };
  }

  async function lawStatute(id) {
    // id is a govinfo package or granule id (USCODE-.../CFR-.../PLAW-.../etc).
    const gid = String(id);
    // derive the package id: strip granule suffixes (-sec.., -vol.., -Pg.., -part..)
    let pkg = gid;
    const cut = gid.search(/-(sec|vol|part|chap|subchap|Pg|pt|art)\b/);
    // packages are like USCODE-2008-title28 or WCPD-2008-07-14 (date packages have no granule)
    const mTitle = gid.match(/^([A-Z]+-\d{4}(?:-\d{2}-\d{2})?(?:-title\d+(?:-vol\d+)?)?)/);
    if (cut !== -1 && mTitle) pkg = mTitle[1];
    else if (cut !== -1) pkg = gid.slice(0, cut);

    const KEY = GOVINFO_KEY ? "?api_key=" + encodeURIComponent(GOVINFO_KEY) : "";
    // 1) the API text endpoints are the reliable source (raw text, not the nav page)
    const apiTries = [
      "https://api.govinfo.gov/packages/" + gid + "/htm" + KEY,        // granule-as-package
      "https://api.govinfo.gov/packages/" + pkg + "/htm" + KEY,        // package text
    ];
    // 2) content-file paths as a fallback: pkg in path, granule in filename
    const contentTries = [
      "https://www.govinfo.gov/content/pkg/" + pkg + "/html/" + gid + ".htm",
      "https://www.govinfo.gov/content/pkg/" + pkg + "/html/" + pkg + ".htm",
    ];

    function looksLikeChrome(t) {
      // the details page is all navigation; reject it
      const head = t.slice(0, 1500).toLowerCase();
      return head.indexOf("skip to main content") !== -1 ||
        (head.indexOf("browse a to z") !== -1 && head.indexOf("\u00a7") === -1) ||
        head.indexOf("<!doctype html") !== -1 && head.indexOf("govinfo") !== -1 && t.length < 4000;
    }
    function clean(raw) {
      let txt = raw.replace(/<head[\s\S]*?<\/head>/i, "")
        .replace(/<script[\s\S]*?<\/script>/gi, "")
        .replace(/<style[\s\S]*?<\/style>/gi, "")
        .replace(/<\/(p|div|h\d|tr|li|pre)>/gi, "\n")
        .replace(/<br\s*\/?>/gi, "\n")
        .replace(/<[^>]+>/g, "")
        .replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">")
        .replace(/&quot;/g, '"').replace(/&#39;/g, "'").replace(/&nbsp;/g, " ")
        .replace(/[ \t]+/g, " ").replace(/\n{3,}/g, "\n\n").trim();
      return txt;
    }

    let txt = "";
    for (const u of apiTries.concat(contentTries)) {
      try {
        const raw = await tfetch(u, 18000);
        if (!raw) continue;
        if (looksLikeChrome(raw)) continue; // skip the nav page
        const c = clean(raw);
        if (c && c.length > 80) { txt = c; break; }
      } catch (e) { /* try next */ }
    }
    if (!txt) {
      const e = new Error("nohtml"); e.nohtml = true;
      e.detail = "https://www.govinfo.gov/app/details/" + pkg;
      throw e;
    }
    return { id: gid, text: txt.slice(0, 1200000),
      detail: "https://www.govinfo.gov/app/details/" + pkg };
  }
  function annexRoute(kind, q, res) {
    const qstr = String(q.searchParams.get("q") || "").trim().slice(0, 160);
    if (kind === "statute") {
      const id = String(q.searchParams.get("id") || "").replace(/[^A-Za-z0-9\-]/g, "").slice(0, 80);
      if (!id) return send(res, 400, { error: "id required" }, true);
      const file = path.join(ANNEX_DIR, "statute-" + id + ".json");
      const hit = annexCache(file);
      if (hit) return send(res, 200, hit);
      return lawStatute(id).then((out) => { annexSave(file, out); send(res, 200, out); })
        .catch((e) => e && e.nohtml
          ? send(res, 200, { id, text: "", detail: e.detail })
          : send(res, 502, { error: "statute fetch failed: " + e.message }, true));
    }
    if (kind === "docket") {
      const id = String(q.searchParams.get("id") || "").replace(/[^0-9]/g, "").slice(0, 12);
      if (!id) return send(res, 400, { error: "id required" }, true);
      const file = path.join(ANNEX_DIR, "docket-" + id + ".json");
      const hit = annexCache(file);
      if (hit) return send(res, 200, hit);
      return lawDocket(id).then((out) => { annexSave(file, out); send(res, 200, out); })
        .catch((e) => send(res, 502, { error: "docket fetch failed: " + e.message }, true));
    }
    if (kind === "case") {
      const id = String(q.searchParams.get("id") || "").replace(/[^0-9]/g, "").slice(0, 12);
      if (!id) return send(res, 400, { error: "id required" }, true);
      const file = path.join(ANNEX_DIR, "case-" + id + ".json");
      const hit = annexCache(file);
      if (hit) return send(res, 200, hit);
      return lawCase(id).then((out) => { annexSave(file, out); send(res, 200, out); })
        .catch((e) => e && e.rate
          ? send(res, 429, { error: "the law library is busy \u2014 try again in a moment" }, true)
          : send(res, 502, { error: "case fetch failed: " + e.message }, true));
    }
    if (qstr.length < 2) return send(res, 400, { error: "q required" }, true);
    const slug = qstr.toLowerCase().replace(/[^a-z0-9]+/g, "-").slice(0, 70);
    const file = path.join(ANNEX_DIR, kind + "-" + slug + ".json");
    const hit = annexCache(file);
    if (hit) return send(res, 200, hit);
    const p = kind === "papers" ? researchSearch(qstr) : lawSearch(qstr);
    p.then((out) => { annexSave(file, out); send(res, 200, out); })
      .catch((e) => send(res, 502, { error: kind + " search failed: " + e.message }, true));
  }

  const routes = {
    "/wing/info": (q, res) => {
      const out = {};
      for (const w of Object.keys(wings))
        out[w] = { subjects: wings[w].subjects, built: wings[w].built, version: wings[w].version };
      send(res, 200, { wings: out });
    },
    "/wing/room": (q, res) => {
      const w = String(q.searchParams.get("wing") || "");
      const wing = wings[w];
      if (!wing) return send(res, 404, { error: "no such wing \u2014 its index hasn't been built" }, true);
      const n = wing.subjects.length || 1;
      const si = Math.max(0, Math.min(n - 1, parseInt(q.searchParams.get("si"), 10) || 0));
      const depth = Math.max(0, parseInt(q.searchParams.get("depth"), 10) || 0);
      const rows = wing.qRoom.all(si, depth * ROOM_SIZE, (depth + 1) * ROOM_SIZE);
      const books = rows.map((r) => ({
        id: r.id, title: r.title, author: r.authors, year: r.year,
        cited: r.cited, pdf: r.pdf || null, url: r.url || null,
      }));
      while (books.length < ROOM_SIZE) books.push(null);
      send(res, 200, { wing: w, si, depth, got: rows.length, books });
    },
    "/annex/papers": (q, res) => annexRoute("papers", q, res),
    "/annex/law": (q, res) => annexRoute("law", q, res),
    "/annex/case": (q, res) => annexRoute("case", q, res),
    "/annex/statute": (q, res) => annexRoute("statute", q, res),
    "/annex/docket": (q, res) => annexRoute("docket", q, res),
    "/audio/resolve": (q, res) => audioRoute(q, res),
    "/gutenberg-find": (q, res) => {
      const title = String(q.searchParams.get("title") || "").slice(0, 200).trim();
      const author = String(q.searchParams.get("author") || "").slice(0, 120).trim();
      if (title.length < 2) return send(res, 400, { error: "title required" }, true);
      pgFind(title, author).then((out) => send(res, 200, out))
        .catch((e) => send(res, 502, { error: "pg catalog lookup failed: " + e.message }, true));
    },
    "/health": (q, res) => send(res, 200, { ok: true }, true),
    "/version": (q, res) => send(res, 200, {
      version: fs.existsSync(VER_PATH) ? fs.readFileSync(VER_PATH, "utf8").trim() : metaRows.built,
      built: metaRows.built,
      works: Number(metaRows.works || 0),
    }),
    "/meta": (q, res) => send(res, 200, {
      built: metaRows.built, works: Number(metaRows.works || 0),
      cap: Number(metaRows.cap || 0), subjects: SUBJECTS, subject_counts: subjectCounts,
    }),
    "/room": (q, res) => {
      const si = Math.max(0, Math.min(63, parseInt(q.searchParams.get("si"), 10) || 0));
      const depth = Math.max(0, parseInt(q.searchParams.get("depth"), 10) || 0);
      const rows = qRoom.all(si, depth * ROOM_SIZE, (depth + 1) * ROOM_SIZE);
      const books = rows.map(doc);
      while (books.length < ROOM_SIZE) books.push(null);
      send(res, 200, { si, depth, got: rows.length, books });
    },
    "/search": (q, res) => {
      const raw = String(q.searchParams.get("q") || "").trim().slice(0, 120);
      const limit = Math.max(1, Math.min(40, parseInt(q.searchParams.get("limit"), 10) || 18));
      if (raw.length < 2) return send(res, 200, { docs: [] });
      // articles and glue-words would force-exclude exact titles that lack them
      // ("The Metamorphosis" must find works titled plain "Metamorphosis")
      const STOP = new Set(["the", "a", "an", "of", "and", "or", "in", "on", "to"]);
      const all = raw.split(/\s+/).filter(Boolean).slice(0, 8);
      let toks = all.filter((t) => !STOP.has(t.toLowerCase()));
      if (!toks.length) toks = all;
      const terms = toks.map((t) => '"' + t.replace(/"/g, "") + '"').join(" ");
      let rows = [];
      try { rows = qFts.all(terms, limit); } catch (e) { rows = []; }
      if (!rows.length) rows = qLike.all("%" + raw + "%", limit);
      send(res, 200, { docs: rows.map(doc) });
    },
    "/random": (q, res) => {
      if (!nonEmptySis.length) return send(res, 404, { error: "the stacks are empty" });
      const si = nonEmptySis[Math.floor(Math.random() * nonEmptySis.length)];
      const n = qCount.get(si).n;
      const rank = Math.floor(Math.random() * Math.min(n, 5000));
      const r = qAt.get(si, rank);
      if (!r) return send(res, 404, { error: "not found" });
      send(res, 200, { si, depth: Math.floor(rank / ROOM_SIZE), slot: rank % ROOM_SIZE, doc: doc(r) });
    },
  };

  http.createServer((req, res) => {
    const q = new URL(req.url, "http://x");
    const m = q.pathname.match(/^\/work\/(OL\d+W)$/i);
    if (m) {
      const r = qWork.get("/works/" + m[1].toUpperCase());
      return r ? send(res, 200, doc(r)) : send(res, 404, { error: "not found" });
    }
    const g = q.pathname.match(/^\/gutenberg\/(\d{1,7})$/);
    if (g) return gutenbergRoute(g[1], res);
    const h = routes[q.pathname];
    if (h) return h(q, res);
    send(res, 404, { error: "no such route" });
  }).listen(PORT, () => console.log("stacks-catalog serving on :" + PORT +
    " — " + metaRows.works + " works, snapshot " + metaRows.built));
}

bootstrap()
  .then(bootstrapWings)
  .then(startServer)
  .catch((e) => { console.error("bootstrap failed:", e.message); process.exit(1); });
