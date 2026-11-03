# PIOU AI

A private, shareable chat app built with Flask + Gemini (Groq fallback).

- General-purpose chat in English and Greek
- Image understanding (up to 9 images per message)
- Guaranteed “Okay daddy PIOU” prefix
- Invite-code login
- Per-IP and global daily rate limits
- Runs locally or on Render / Railway / Docker

---

## Local use

```powershell
pip install -r requirements.txt
python server.py
```

Open <http://127.0.0.1:5000/>.

Settings live in `.env` (already created, **never commit it**):

```ini
GEMINI_API_KEY=...
GROQ_API_KEY=...
PIOU_ACCESS_CODE=
SECRET_KEY=...
```

With `PIOU_ACCESS_CODE` empty, local access is open. Set a code to require login.

### Share on the same Wi-Fi

```powershell
$env:HOST="0.0.0.0"
$env:PIOU_ACCESS_CODE="your-code"
python server.py
```

Others on your network open `http://YOUR-PC-IP:5000/`. This is not reachable from the internet.

### Production mode locally

```powershell
$env:PIOU_PRODUCTION="1"
python server.py
```

This serves the app with Waitress instead of Flask’s development server.

---

## Deploy to Render (free)

1. Put this folder in a GitHub repository (`.env` is ignored).
2. In Render: **New → Blueprint**, pick the repo. Render reads `render.yaml`.
3. Set these secrets when prompted:
   - `GEMINI_API_KEY`
   - `GROQ_API_KEY`
   - `PIOU_ACCESS_CODE` — the invite code you give people
4. Deploy. Render gives you a URL like `https://piou-ai.onrender.com`.

Free instances sleep after inactivity, so the first visit may take ~30–60 seconds.

### Railway / Heroku

Use the included `Procfile`:

```
web: waitress-serve --listen=0.0.0.0:$PORT server:app
```

### Docker

```bash
docker build -t piou-ai .
docker run -p 8000:8000 --env-file .env piou-ai
```

---

## Access code and limits

| Setting | Default | Meaning |
|---|---|---|
| `PIOU_ACCESS_CODE` | empty | Shared invite code. Empty = no login. |
| `PIOU_GUEST_CODE` | empty | Optional temporary code for guests. |
| `PIOU_GUEST_UNTIL` | empty | When the guest code expires (ISO 8601 UTC, or Unix time). |
| `PIOU_RATE_PER_MINUTE` | 20 | Requests per IP per minute |
| `PIOU_RATE_PER_DAY` | 150 | Requests per IP per day |
| `PIOU_GLOBAL_PER_DAY` | 500 | Total requests per day across everyone |
| `PIOU_LOGIN_PER_MINUTE` | 10 | Access-code attempts per IP per minute |

Each image costs extra units (text = 1, each image = +2), so image spam is limited faster than text.

Counters are stored in memory and reset when the server restarts. They work because the app runs as **one process**. If you scale to multiple workers, move the counters to Redis.

---

## Can Google find it?

Only after it is deployed to a public URL **and** Google crawls it. To speed that up:

1. Add the URL to [Google Search Console](https://search.google.com/search-console).
2. Request indexing.
3. Link to it from a public page (GitHub profile, social post, etc.).

A brand-new free Render URL may not appear in search results for days or weeks, and it will never appear while the app is only on `127.0.0.1`.

---

## Security notes

- `.env` contains real API keys. It is in `.gitignore`; keep it that way.
- The access code is shared, not per-user. Anyone with it can chat.
- Rotate both API keys if they were ever posted publicly:
  - Gemini: <https://aistudio.google.com/apikey>
  - Groq: <https://console.groq.com/keys>
- Free-tier Gemini/Groq quotas are shared by all visitors. The daily caps exist to protect your quota.
- For real user accounts, add a database and per-user sessions instead of a shared code.
