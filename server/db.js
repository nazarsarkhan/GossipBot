// server/db.js — SQLite bootstrap and helper methods
const path = require('path');
const Database = require('better-sqlite3');

// Database file lives next to the server folder (server/gossip.db)
const DB_FILE = process.env.DB_FILE || path.join(__dirname, 'gossip.db');
const db = new Database(DB_FILE);

db.pragma('journal_mode = WAL');
db.pragma('foreign_keys = ON');

db.exec(`
  CREATE TABLE IF NOT EXISTS submissions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    text       TEXT    NOT NULL,
    lang       TEXT    NOT NULL DEFAULT 'en' CHECK (lang IN ('en','ru')),
    status     TEXT    NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','approved','rejected')),
    ip_hash    TEXT,
    ua         TEXT,
    created_at DATETIME NOT NULL DEFAULT (datetime('now'))
  );
  CREATE INDEX IF NOT EXISTS idx_submissions_status_created
    ON submissions(status, created_at DESC);
`);

const insertStmt = db.prepare(`
  INSERT INTO submissions (text, lang, ip_hash, ua)
  VALUES (@text, @lang, @ip_hash, @ua)
`);
const listLatestStmt = db.prepare(`
  SELECT id, text, lang, status, created_at
  FROM submissions
  ORDER BY created_at DESC
  LIMIT @limit
`);

module.exports = {
  insertSubmission(payload) {
    const info = insertStmt.run(payload);
    return info.lastInsertRowid;
  },
  listLatest(limit = 100) {
    return listLatestStmt.all({ limit });
  }
};
