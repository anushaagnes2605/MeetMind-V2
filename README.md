# MeetMind V2

MeetMind converts meeting transcripts into decisions, action items, conflicts and team memory.

## Run locally

Backend:

```powershell
cd backend
.\\venv\\Scripts\\Activate.ps1
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

For local development, copy `backend/.env.example` to `backend/.env`. The development-only demo account defaults to `admin@meetmind.demo` and `MeetMind@123`, and can be changed with `DEMO_ADMIN_EMAIL` and `DEMO_ADMIN_PASSWORD`. Set `MEETMIND_ENV=production` to disable demo-account seeding. Login sessions use HTTP-only cookies and require the frontend origin configured in CORS.

## Deployment configuration

Set `VITE_API_URL` at frontend build time to the deployed backend origin. Set `CORS_ORIGINS` in the backend environment to a comma-separated list of deployed frontend origins. Set `MEETMIND_ENV=production` and provide provider credentials and a secure production configuration. Do not include API keys in frontend environment files or build artifacts.
