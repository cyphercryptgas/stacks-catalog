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

// ---------------------------------------------------------------- serving
function startServer() {
  const db = new DatabaseSync(DB_PATH, { readOnly: true });
  const metaRows = Object.fromEntries(
    db.prepare("SELECT k, v FROM meta").all().map((r) => [r.k, r.v]));
  const SUBJECTS = JSON.parse(metaRows.subjects || "[]");
  const subjectCounts = JSON.parse(metaRows.subject_counts || "{}");

  const qRoom = db.prepare(
    "SELECT w.* FROM subject_rank sr JOIN works w ON w.id = sr.wid " +
    "WHERE sr.si = ? AND sr.rank >= ? AND sr.rank < ? ORDER BY sr.rank");
  const qWork = db.prepare("SELECT * FROM works WHERE key = ?");
  const qFts = db.prepare(
    "SELECT w.* FROM fts JOIN works w ON w.id = fts.rowid WHERE fts MATCH ? ORDER BY rank LIMIT ?");
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

  const routes = {
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
      const terms = raw.split(/\s+/).filter(Boolean).slice(0, 8)
        .map((t) => '"' + t.replace(/"/g, "") + '"').join(" ");
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
    const h = routes[q.pathname];
    if (h) return h(q, res);
    send(res, 404, { error: "no such route" });
  }).listen(PORT, () => console.log("stacks-catalog serving on :" + PORT +
    " — " + metaRows.works + " works, snapshot " + metaRows.built));
}

bootstrap()
  .then(startServer)
  .catch((e) => { console.error("bootstrap failed:", e.message); process.exit(1); });
