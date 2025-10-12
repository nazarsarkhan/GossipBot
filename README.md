# Goxxip Box

Goxxip Box is a full-stack toolkit for running an anonymous submission box with a Telegram moderation workflow. The project contains:

- **Static single-page frontend (`index.html`)** – polished submission form with language and theme switchers.
- **Node.js API (`server/server.js`)** – Express 5 service that stores submissions in MongoDB and serves the static frontend.
- **Telegram moderation bot (`server/gossip_bot.py`)** – Aiogram 3 bot that lets moderators review, publish, and reject submissions.

This document walks you through local development, production hosting, and daily operations.

---

## 1. Repository structure

```
.
├── index.html               # Frontend entry point
├── img/                     # Frontend assets
├── server/
│   ├── server.js            # Express API + static hosting
│   ├── gossip_bot.py        # Telegram moderation bot
│   ├── requirements.txt     # Python dependencies for the bot
│   ├── package.json         # Node.js dependencies for the API
│   └── db.js                # Optional SQLite helper (legacy/testing)
├── .env.example             # Sample environment configuration
└── README.md                # This guide
```

The MongoDB-backed flow is the canonical path. The SQLite helper is kept for legacy testing only and is not wired into the default server code.

---

## 2. Prerequisites

| Component           | Version (tested) | Notes |
|---------------------|------------------|-------|
| Node.js             | ≥ 20             | Express 5 requires a modern runtime. |
| npm                 | ≥ 9              | Ships with recent Node.js releases. |
| Python              | ≥ 3.11           | Required for Aiogram 3. |
| MongoDB             | ≥ 6.0            | Replica set or standalone; Atlas works fine. |

Optional tooling: Docker (for local MongoDB), `pm2` (process manager for the API), and `systemd`/`supervisor` for bot hosting.

---

## 3. Configuration

1. Copy `.env.example` and fill in real credentials:
   ```bash
   cp .env.example .env
   ```
2. Update the following keys:

   | Variable         | Description |
   |------------------|-------------|
   | `MONGODB_URI`    | Connection string with credentials. Shared by API and bot. |
   | `DB_NAME`        | Database name (`gossip` by default). |
   | `COLLECTION`     | Collection where submissions are stored. |
   | `PORT`           | HTTP port for the Express server. |
   | `FRONTEND_ORIGIN`| Optional. Set to your production domain to lock down CORS. |
   | `BOT_TOKEN`      | Telegram bot token issued by @BotFather. |
   | `CHANNEL_ID`     | Target channel (numeric ID or `@handle`) for publishing. |
   | `ADMINS`         | Comma-separated Telegram user IDs allowed to moderate. Leave empty to allow everyone. |
   | `POLL_INTERVAL`  | Seconds between automatic publishing checks. Use `0` to disable the background publisher. |
   | `BATCH_LIMIT`    | Maximum messages pushed per interval. |
   | `LOG_LEVEL`      | Bot log level (`INFO`, `DEBUG`, ...). |

3. Export the same `.env` file for both services (`server/server.js` and `server/gossip_bot.py`).

---

## 4. Installing dependencies

```bash
# Install Node.js dependencies for the API
cd server
npm install

# Install Python dependencies for the Telegram bot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

> Tip: keep the virtual environment inside `server/.venv` so that deployment scripts can reuse it.

---

## 5. Running locally

1. **Start MongoDB** – either via Docker or a local installation.
   ```bash
   docker run --rm -d -p 27017:27017 --name gossip-mongo mongo:7
   ```
2. **Start the Express API and static frontend**
   ```bash
   cd server
   npm start
   ```
   The server listens on `http://localhost:3000` by default and serves `index.html` along with the API endpoints.
3. **Run the Telegram bot (optional for local testing)**
   ```bash
   cd server
   source .venv/bin/activate
   python gossip_bot.py
   ```
   The bot connects to Telegram, subscribes to commands, and optionally starts the scheduled publisher if `POLL_INTERVAL > 0` and `CHANNEL_ID` is set.

Use tools like [ngrok](https://ngrok.com/) if you want to expose the local API for mobile testing.

---

## 6. Production deployment

### 6.1 Deploying the Express API

1. Provision a host (VPS, container platform, or serverless environment) with Node.js ≥ 20.
2. Copy the repository, install dependencies (`npm ci` is recommended in CI/CD), and place your `.env` file next to `server/server.js`.
3. Start the service with a process manager:
   ```bash
   cd /srv/goxxip-box/server
   npm ci
   PORT=8080 pm2 start server.js --name goxxip-api
   pm2 save
   ```
4. Configure your reverse proxy (Nginx, Caddy, etc.) to forward HTTPS traffic to the chosen port.
5. Enable log rotation or use pm2-logrotate to keep logs manageable.

### 6.2 Hosting the Telegram bot

1. Ensure the same `.env` file is available (bot requires MongoDB access).
2. Create a dedicated virtual environment and install dependencies:
   ```bash
   cd /srv/goxxip-box/server
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   ```
3. Use `systemd` (or Supervisor) to keep the bot alive. Example unit file:
   ```ini
   [Unit]
   Description=Goxxip Telegram bot
   After=network.target

   [Service]
   Type=simple
   WorkingDirectory=/srv/goxxip-box/server
   EnvironmentFile=/srv/goxxip-box/.env
   ExecStart=/srv/goxxip-box/server/.venv/bin/python gossip_bot.py
   Restart=always
   RestartSec=5

   [Install]
   WantedBy=multi-user.target
   ```
4. Reload systemd, enable, and start the service:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now goxxip-bot
   ```
5. Monitor logs with `journalctl -u goxxip-bot -f`.

The bot logs meaningful information (startup details, publishing events, errors) to stdout using Python’s logging subsystem, which plays well with systemd or container log collectors.

---

## 7. API reference

| Method | Endpoint        | Description |
|--------|-----------------|-------------|
| GET    | `/api/health`   | Returns `{ ok: true, db: boolean }`. Used for health checks and uptime monitoring. |
| POST   | `/api/submit`   | Accepts `{ text, lang }` JSON or form data. Validates length, filters basic PII, hashes IP for abuse mitigation, and returns the MongoDB ObjectId as plain text. |
| GET    | `/api/admin/list` | Development helper that lists recent submissions. Protect or disable in production. |

The API enforces rate limiting (20 requests per minute per IP) and optional CORS restrictions via `FRONTEND_ORIGIN`.

---

## 8. Telegram commands

| Command        | Description |
|----------------|-------------|
| `/start`       | Displays a quick help message. |
| `/pending`     | Lists the latest submissions with `pending` status. |
| `/latest`      | Lists the newest submissions regardless of status. |
| `/publish <id>`| Publishes a submission to the configured channel and marks it as `published`. |
| `/reject <id>` | Marks a submission as `rejected`. |

Only users listed in `ADMINS` may execute moderation commands. Leave `ADMINS` empty to allow any user who can interact with the bot.

The optional background publisher posts `approved` submissions automatically when `POLL_INTERVAL > 0`.

---

## 9. Data model

MongoDB documents stored in `COLLECTION` share this schema:

```json
{
  "_id": ObjectId,
  "text": "string",        // Submission body
  "lang": "en" | "ru",    // UI language selected by the user
  "status": "pending" | "approved" | "rejected" | "published",
  "created_at": ISODate,
  "ip_hash": "string | null", // Truncated SHA-256 hash of the submitter's IP
  "ua": "string"              // User-Agent (first 180 characters)
}
```

MongoDB indexes (`status`, `created_at`) are created automatically during startup.

---

## 10. Customising the frontend

- Update copy and language options in `index.html` (search for the `translations` object around line 380).
- Replace assets in the `img/` folder as needed.
- Deploy static assets through the Express server or any CDN. If you host the frontend elsewhere, point its `fetch` requests to the deployed API URL and adjust `FRONTEND_ORIGIN` accordingly.

---

## 11. Maintenance checklist

- Keep dependencies up to date (`npm outdated`, `pip list --outdated`).
- Rotate the Telegram bot token if leaked.
- Back up MongoDB regularly (e.g., `mongodump` or managed snapshots).
- Monitor process logs and `/api/health` metrics for uptime.

Happy moderating!
