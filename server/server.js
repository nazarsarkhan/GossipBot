// server/server.js — static + API (Express 5)
require('dotenv').config();

const express = require('express');
const cors = require('cors');
const helmet = require('helmet');
const rateLimit = require('express-rate-limit');
const crypto = require('crypto');
const path = require('path');
const { MongoClient } = require('mongodb');

const app = express();

// ---- ENV ----
const PORT = process.env.PORT || 3000;
const FRONTEND_ORIGIN = process.env.FRONTEND_ORIGIN; // e.g. http://localhost:5500
const MONGODB_URI = process.env.MONGODB_URI;
const DB_NAME = process.env.DB_NAME || 'gossip';
const COLLECTION = process.env.COLLECTION || 'submissions';

if (!MONGODB_URI) {
  console.error('MONGODB_URI is not set'); // keep running for /static 404 debugging
}

// ---- Mongo (connect once, reuse) ----
let mongoClient;
let col;
async function getCollection() {
  if (col) return col;
  if (!MONGODB_URI) throw new Error('MONGODB_URI is not set');
  mongoClient = new MongoClient(MONGODB_URI, {
    maxPoolSize: 10,
    serverSelectionTimeoutMS: 10_000,
  });
  await mongoClient.connect();
  const db = mongoClient.db(DB_NAME);
  col = db.collection(COLLECTION);
  await col.createIndex({ status: 1, created_at: -1 });
  await col.createIndex({ created_at: -1 });
  // sanity ping
  await mongoClient.db('admin').command({ ping: 1 });
  return col;
}
async function closeMongo() {
  try { await mongoClient?.close(); } catch {}
  mongoClient = null;
  col = null;
}
process.on('SIGINT', async () => { await closeMongo(); process.exit(0); });
process.on('SIGTERM', async () => { await closeMongo(); process.exit(0); });

// ---- Middlewares ----
app.use(helmet({
  contentSecurityPolicy: false,           // keep simple for inline styles/scripts in your single-file HTML
  crossOriginEmbedderPolicy: false,       // avoid COEP issues for dev
}));
app.use(express.urlencoded({ extended: false, limit: '10kb' }));
app.use(express.json({ limit: '10kb' }));

// CORS: strict if FRONTEND_ORIGIN provided, otherwise allow all (dev)
app.use(cors({
  origin: FRONTEND_ORIGIN ? [FRONTEND_ORIGIN] : '*',
  methods: ['GET', 'POST', 'OPTIONS'],
}));

// Anti-abuse for /api/*
app.use('/api/', rateLimit({
  windowMs: 60_000,
  max: 20,
  standardHeaders: true,
  legacyHeaders: false,
  // do NOT enable `trust proxy` here to avoid bypass warnings
}));

// ---- API ----
const risky = /(\+?\d[\d\s\-()]{8,})|([\w.+-]+@[\w-]+\.[a-z]{2,})|(t\.me\/)/i;

app.get('/api/health', async (_req, res) => {
  try {
    if (!MONGODB_URI) return res.json({ ok: true, db: false });
    const c = await getCollection();
    // cheap query to ensure ready
    await c.estimatedDocumentCount({ maxTimeMS: 5000 });
    res.json({ ok: true, db: true });
  } catch (e) {
    console.error('health error:', e.message);
    res.status(500).json({ ok: false, db: false });
  }
});

app.post('/api/submit', async (req, res) => {
  try {
    const c = await getCollection();

    const text = (req.body?.text || '').toString().trim();
    const lang = (req.body?.lang === 'ru') ? 'ru' : 'en';

    // basic validation
    if (text.length < 10 || text.length > 2000) {
      return res.status(400).type('text').send('Invalid length');
    }
    if (risky.test(text)) {
      return res.status(400).type('text').send('PII detected');
    }

    // minimal privacy: hash IP
    const ip =
      (req.headers['x-forwarded-for'] || '').toString().split(',')[0].trim() ||
      req.socket.remoteAddress || '';
    const ip_hash = ip ? crypto.createHash('sha256').update(ip).digest('hex').slice(0, 16) : null;
    const ua = String(req.get('user-agent') || '').slice(0, 180);

    const doc = {
      text,
      lang,
      status: 'pending',
      created_at: new Date(),
      ip_hash,
      ua,
    };

    const result = await c.insertOne(doc);
    // frontend reads res.text(); return insertedId as text
    return res.status(201).type('text').send(result.insertedId.toString());
  } catch (e) {
    console.error('[submit] error:', e);
    return res.status(500).type('text').send('DB error');
  }
});

// dev-only endpoint to inspect latest docs (remove or protect in prod)
app.get('/api/admin/list', async (_req, res) => {
  try {
    const c = await getCollection();
    const items = await c.find({})
      .sort({ created_at: -1 })
      .limit(50)
      .project({ text: 1, lang: 1, status: 1, created_at: 1 })
      .toArray();
    res.json({ ok: true, items });
  } catch (e) {
    console.error('[admin/list] error:', e);
    res.status(500).json({ ok: false });
  }
});

// ---- Static ----
const ROOT_DIR = path.resolve(__dirname, '..'); // repo root with index.html
app.use(express.static(ROOT_DIR, { index: 'index.html', extensions: ['html', 'htm'] }));

// SPA fallback (Express 5 friendly) — do not intercept /api/*
app.use((req, res, next) => {
  if (req.path.startsWith('/api/')) return next();
  res.sendFile(path.join(ROOT_DIR, 'index.html'), (err) => { if (err) next(err); });
});

// ---- Errors ----
app.use((err, _req, res, _next) => {
  console.error('[UNCAUGHT]', err);
  if (!res.headersSent) res.status(500).send('Server error');
});

// ---- Start ----
app.listen(PORT, () => {
  console.log(`API + Static at http://localhost:${PORT}`);
});
