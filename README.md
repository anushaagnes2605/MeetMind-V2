# MeetMind V2

MeetMind converts meeting transcripts into decisions, action items, conflicts and team memory.

## Run locally

Backend:

```powershell
cd backend
.\venv\Scripts\Activate.ps1
uvicorn main:app --reload --reload-dir . --host 127.0.0.1 --port 8001
```

Frontend:

```powershell
npm install
npm run dev
```

Copy `backend/.env.example` to `backend/.env` and configure provider credentials. `GROQ_API_KEY` enables Groq as the primary provider; `GROQ_MODEL` defaults to `openai/gpt-oss-120b` and `GROQ_TIMEOUT_MS` defaults to `30000`. Gemini remains the secondary provider through `GEMINI_API_KEY`, `GEMINI_MODEL`, and `GEMINI_TIMEOUT_MS`. Keep provider keys in the backend environment file only. Meeting results are labeled `groq`, `gemini`, or `deterministic_rules` and fall back safely when providers fail.

Copy `.env.example` to `.env` if you need to override `VITE_API_URL`.

## Authentication

For local development, copy `backend/.env.example` to `backend/.env`. The development-only demo account defaults to `admin@meetmind.demo` and `MeetMind@123`, and can be changed with `DEMO_ADMIN_EMAIL` and `DEMO_ADMIN_PASSWORD`. Set `MEETMIND_ENV=production` to disable demo-account seeding. On Vercel the app treats the deployment as production unless `MEETMIND_ENV` says otherwise, so demo seeding never runs there by accident.

Users can register from the login screen (`POST /auth/register`). Login sessions use HTTP-only cookies stored in the database, so they survive browser refreshes and server restarts. In production the cookie is `Secure` and `SameSite=None` so a frontend and backend on different Vercel domains can exchange it; set `COOKIE_SAMESITE`/`COOKIE_SECURE` only if you deliberately override that.

A local account only exists in `backend/meetmind.db`. It is not in the hosted database, so a locally registered user cannot log in to the deployed app until the same account exists there (register it through the deployed UI, or use `backend/account_tool.py`).

### Checking an account against the deployed database

`backend/account_tool.py` runs the same lookup and password verification as `POST /auth/login`, and prints whether the account exists and whether the password matches without ever printing the password or its hash:

```powershell
cd backend
$env:TURSO_DATABASE_URL="libsql://..."
$env:TURSO_AUTH_TOKEN="..."
python account_tool.py --email admin@meetmind.demo --require-turso
```

The password is prompted for (or read from `MEETMIND_ACCOUNT_PASSWORD`). Add `--create` to provision a missing account with that same password, or `--reset-password` to deliberately replace the hash of an existing one. `--require-turso` refuses to inspect the local SQLite file by mistake.

## Storage

Local development uses `backend/meetmind.db`. **SQLite cannot be used on Vercel**: the function filesystem is read-only except for `/tmp`, and `/tmp` is wiped between invocations, which deletes every registered user and session. The backend therefore requires a hosted Turso database when it runs on Vercel:

1. Create a database at https://turso.tech.
2. Copy its URL (`libsql://...` or `turso://...`) and an auth token.
3. Set `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` on the backend Vercel project.

When those variables are set every query goes to Turso. `DATABASE_PATH` is only for local development or a hosted platform with a persistent volume. On Vercel, startup fails with a clear error if neither persistent option is configured.

## Deployment configuration

The backend and frontend are two separate Vercel projects.

### Backend project

- Root Directory: `backend`
- Vercel detects FastAPI from `requirements.txt` and uses `main.py`.
- Environment variables:
  - `TURSO_DATABASE_URL` — Turso database URL (required)
  - `TURSO_AUTH_TOKEN` — Turso auth token (required)
  - `CORS_ORIGINS` — the exact frontend origin, for example `https://meetmind.vercel.app` (required, otherwise the browser blocks the login request)
  - `MEETMIND_ENV` — optional; Vercel already defaults to production behaviour
  - `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD` — optional, creates a missing account in the hosted database on startup; existing accounts are never overwritten. Remove them after the first successful deploy.
  - `GROQ_API_KEY`, `GEMINI_API_KEY` and their optional model/timeout variables — AI provider credentials
- Do not set `DATABASE_PATH` on Vercel.

### Frontend project

- Root Directory: repository root
- Build command: `npm run build`
- Environment variable:
  - `VITE_API_URL` — the backend project URL, for example `https://meetmind-api.vercel.app`, with no trailing slash. Vite bakes this in at build time, so redeploy after changing it.

Do not put API keys or database tokens in `VITE_*` variables; Vite exposes them in the browser bundle.

### Redeploy and verify

1. Add the backend environment variables and redeploy the backend project.
2. Confirm `https://<backend>/health` returns `{"status":"healthy"}`.
3. Confirm the account exists in the hosted database with `python account_tool.py --email <email> --require-turso` (add `--create` once if it is missing).
4. Add `VITE_API_URL` to the frontend project and redeploy it.
5. Sign in with that account, or create a new account from the login screen.
6. Refresh the page; the session should remain active. Then restart/redeploy the backend and refresh again to confirm the user still exists.
