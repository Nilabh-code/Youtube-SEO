# TubeForge — YouTube Assistant

Paste any YouTube video or Short link → the app reads the transcript → Groq writes
**3 fresh titles, descriptions and tag sets** you can paste straight into YouTube Studio.

Built with **FastAPI + vanilla HTML/CSS/JS**, deployed on **Render**.

---

## Features

- Works with `youtube.com/watch`, `youtu.be`, `/shorts/`, `/embed/`, `/live/` and raw video IDs
- Transcript auto-detect (English first, then whatever captions exist) with a watch-page fallback
- Automatic browser fallback: if the server IP is blocked, the site pulls caption
  URLs from the server and downloads the captions from the visitor's own browser
  (home IPs are not blocked) — still paste-URL-only, no action needed
- Optional free Supadata key: server fetches transcripts itself on hosts YouTube blocks
- Built-in manual transcript paste: if YouTube blocks the server IP, the site guides you to paste the transcript and still generates everything
- Optional `TRANSCRIPT_PROXY` env var for hosts whose IP YouTube blocks
- 2–5 metadata variations, each from a different angle (SEO / curiosity / community)
- Copy buttons everywhere, **Copy all** and **Download .txt**
- Tag chips + total tag character counter (YouTube's 500 char limit)
- Original transcript viewer with copy
- Animated, phone-friendly responsive UI
- `/api/health` endpoint for Render's health checks

---

## 1. Get a Groq API key

1. Go to <https://console.groq.com> and sign in.
2. **API Keys → Create API Key**, copy it (`gsk_...`).
3. The default model is `llama-3.3-70b-versatile`. To use another one, set
   `GROQ_MODEL` (for example `llama-3.1-8b-instant`).

## 1b. Get a free Supadata API key (URL-only transcripts on Render)

YouTube blocks transcript requests coming from cloud servers like Render, so
without this step visitors must paste the transcript manually when blocked.
A free Supadata key fixes that — the server fetches the transcript itself and
the site stays paste-URL-only:

1. Sign up at <https://supadata.ai> (free plan, no card required).
2. Open the dashboard and copy your API key (`dash.supadata.ai/organizations/api-key`).
3. Add it as `SUPADATA_API_KEY` in Render → your service → **Environment**
   (mark it secret), then redeploy. Locally, put it in `.env`.

Without the key the app still works: it tries YouTube directly and falls back
to the built-in paste-transcript panel.

## 2. Run locally

```bash
git clone <your-repo-url>
cd youtube-assistant

python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # macOS / Linux

pip install -r requirements.txt

copy .env.example .env        # then paste your key into .env   (macOS/Linux: cp .env.example .env)
uvicorn main:app --reload
```

Open <http://127.0.0.1:8000>.

## 3. Deploy on Render (GitHub)

1. Create a new GitHub repo and push this folder:

   ```bash
   git init
   git add .
   git commit -m "YouTube assistant"
   git branch -M main
   git remote add origin https://github.com/<you>/youtube-assistant.git
   git push -u origin main
   ```

2. On [render.com](https://dashboard.render.com): **New → Web Service → Connect repository**.
3. Render picks up `render.yaml` automatically (build/start commands and health check).
   If you prefer to fill it in by hand:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Health Check Path:** `/api/health`
4. Under **Environment**, add:

   | Key | Value |
   | --- | --- |
   | `GROQ_API_KEY` | `gsk_...` |
   | `GROQ_MODEL` | `llama-3.3-70b-versatile` (optional) |
   | `SUPADATA_API_KEY` | free key from supadata.ai — enables automatic transcripts on Render |
   | `TRANSCRIPT_PROXY` | `http://user:pass@host:port` (optional, only if YouTube blocks the server IP) |

5. Deploy. Your site is live at `https://youtube-assistant.onrender.com`.

> Free-tier services spin down after inactivity, so the first request after idle takes
> ~30 seconds. Upgrade to a paid instance if you want instant responses.

---

## API

### `POST /api/generate`

```json
{ "url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ", "variations": 3 }
```

Success:

```json
{
  "video_id": "aqz-KE-bpKQ",
  "language": "en",
  "model": "llama-3.3-70b-versatile",
  "transcript": "...",
  "variations": [
    { "title": "...", "description": "...", "tags": ["...", "..."] }
  ]
}
```

Errors return `{ "error": "human readable message" }` with a 4xx/5xx status.

### `POST /api/generate-from-text`

Manual-transcript fallback: the user pastes a transcript they copied from YouTube.

```json
{
  "transcript": "full transcript text, at least a few sentences",
  "url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ",
  "variations": 3
}
```

`url` is optional and only used for labelling. Same success shape as `/api/generate`
(`source` is `"manual paste"`).

Errors from `/api/generate` include a machine readable `kind`:

| kind | meaning |
| --- | --- |
| `blocked` | YouTube blocked this server's IP — retry later or paste manually |
| `no_captions` | the video has no captions |
| `unavailable` | private / deleted / region locked / age restricted / bad id |

### `POST /api/tracks`

Returns caption track URLs without downloading them (powers the automatic
browser fallback):

```json
{ "url": "https://www.youtube.com/watch?v=aqz-KE-bpKQ" }
```

```json
{
  "video_id": "aqz-KE-bpKQ",
  "tracks": [{ "languageCode": "en", "label": "English", "is_generated": false, "url": "https://..." }]
}
```

### `GET /api/debug-transcript?url=...`

Step-by-step transcript diagnostics (which source worked/failed and why).
Handy when the deployed server behaves differently from your laptop.

### `GET /api/health`

```json
{ "status": "ok", "groq_key_configured": true, "model": "llama-3.3-70b-versatile" }
```

---

## Project layout

```
├── main.py             FastAPI app + API routes
├── youtube_service.py  URL parsing + transcript chain (library -> Supadata -> watch page)
├── groq_service.py     Groq chat completion → strict JSON metadata
├── requirements.txt
├── render.yaml         Render build/start/health-check config
└── static/
    ├── index.html
    ├── styles.css
    └── app.js
```

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| "The server is missing GROQ_API_KEY" | Add the key in Render → Environment, then redeploy. |
| "Groq rejected the API key" | Regenerate the key in the Groq console. |
| "Model ... is not available" | Set `GROQ_MODEL` to a model you have access to, e.g. `llama-3.1-8b-instant`. |
| "YouTube is rate limiting this server" | Wait ~1 min and retry, or paste the transcript manually when the site offers it. |
| "Captions are turned off for this video" | That video has no transcript — try another, or paste subtitles if you have them. |
| "No captions found automatically" panel | Copy the transcript from YouTube (video → `···` → **Open transcript**), paste it in the box, hit generate. Same results, no extra setup. |
