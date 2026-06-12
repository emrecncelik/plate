# Plate

Voice and text calorie logger. Browser frontend, Flask backend. Each user signs
in with Google and gets their own reference table and daily log, stored in
SQLite. The reference table starts empty.

## Run

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
GOOGLE_CLIENT_ID=your-client-id SECRET_KEY=some-random-string python app.py
```

Open http://localhost:5000

`./setup.sh` creates the venv and installs dependencies; `./setup.sh --run` also
starts the app.

## Google sign-in setup

1. In the Google Cloud Console, create an OAuth 2.0 Client ID of type "Web
   application".
2. Add your origin to "Authorized JavaScript origins": `http://localhost:5000`
   for local use, and your deployed URL for production.
3. Pass the client ID to the app as the `GOOGLE_CLIENT_ID` env var. The frontend
   reads it from `/api/config`.

Environment variables:

| Var | Purpose |
|-----|---------|
| `GOOGLE_CLIENT_ID` | OAuth client ID; required for sign-in |
| `SECRET_KEY`       | signs the session cookie; set to a random string |
| `DATA_DIR`         | where `plate.db` lives; defaults to `./data` |
| `COOKIE_SECURE`    | set to `1` behind HTTPS so the cookie is secure-only |
| `ASR_MODEL`        | `tiny.en`, `base.en` (default), `small.en`, or multilingual |
| `WARM_ASR`         | set to `1` to preload the speech model at startup |

## Voice

The browser records a clip with MediaRecorder and POSTs it to `/api/transcribe`,
where faster-whisper turns it into text. Because transcription runs on the
backend, voice works in any browser. The text is parsed against the signed-in
user's reference table. Matched items are shown for approval; tap an item to add
it.

The first time the mic is used, the model downloads (about 145MB for `base.en`)
and is cached under `~/.cache/huggingface`. It runs on CPU with int8. If
faster-whisper is not installed, the mic is disabled and typing keeps working.

## Layout

```
app.py              backend: auth, SQLite storage, parser, routes
requirements.txt    dependencies
static/index.html   frontend
data/plate.db       per-user reference and log, created on first run
```

## Data

SQLite tables, all scoped by `user_id`:

- `users(id, email, name, created_at)` where `id` is the Google subject id
- `reference(id, user_id, name, cal, protein, unit, aliases)` where `unit` is `g`
  (cal/protein per gram) or `piece` (per item) and `aliases` is a JSON array
- `log(id, user_id, date, meal, name, qty, unit, cal, protein, created_at)`

## API

`/api/config`, `/api/auth/*`, and `/api/asr-status` are public; the rest require
a signed-in session.

| Method | Path | Body / notes |
|--------|------|--------------|
| GET    | `/api/config`                 | `{google_client_id}` |
| POST   | `/api/auth/google`            | `{credential}` Google ID token, sets session |
| GET    | `/api/auth/me`                | current `{user}` or null |
| POST   | `/api/auth/logout`            | clears the session |
| GET    | `/api/reference`              | list the user's food items |
| POST   | `/api/reference`              | `{name, cal, protein, unit}` add item (`protein` optional) |
| DELETE | `/api/reference/<id>`         | remove item |
| POST   | `/api/parse`                  | `{text}` returns parsed preview, nothing saved |
| GET    | `/api/log/<date>`             | day's entries (`date` = `YYYY-MM-DD`) |
| POST   | `/api/log/<date>`             | `{meal, text}` or `{meal, items}`, appends and returns the day |
| DELETE | `/api/log/<date>/<meal>/<id>` | remove one entry |
| GET    | `/api/asr-status`             | whether the speech model is available |
| POST   | `/api/transcribe`             | multipart `audio` file, returns `{text}` |

## Tests

Run the suite before every deployment or feature change:

```bash
./run_tests.sh            # or: python -m unittest discover -s tests
```

It uses an isolated temporary database (never your `data/`) and no network, and
covers parsing, the auth gate, per-user data isolation, logging, friends and
nicknames, daily totals, and the SQLite migration. Add a test alongside any new
behaviour.

## Deploy

The `Dockerfile` bakes in the model and serves the app with gunicorn.
`railway.toml` configures a Railway deploy. On Railway, attach a volume and set
`DATA_DIR` to its mount path so `plate.db` persists, set `GOOGLE_CLIENT_ID`,
`SECRET_KEY`, and `COOKIE_SECURE=1`, and set `WARM_ASR=1` to preload the model.
