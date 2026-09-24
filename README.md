# CAR-T Cart-Order Q&A — Backend + React Frontend

Two sibling folders, expected to sit next to each other exactly as in this zip:

```
project/
├── backend/     FastAPI app (Azure OpenAI orchestrator + tools)
└── frontend/    React (Vite) chat UI
```

## First-time setup

```bash
cd backend
pip install -r requirements.txt
# set the required env vars — see backend/README.md for the full list
# (Azure OpenAI incl. embeddings, New Relic, PostgreSQL CCM/COIC/KB)

cd ../frontend
npm install
```

## Run everything with one command

```bash
cd backend
python app.py
```

This starts:

- **Backend** — FastAPI/uvicorn on **http://localhost:5000** (the `/api/chat` endpoint the UI talks to)
- **Frontend** — the React app via `npm run dev` on **http://localhost:5173**, launched automatically as a child process

Open **http://localhost:5173** in your browser — that's the chat UI. The
React app is configured (see `frontend/vite.config.js`) to proxy its
`/api/*` calls to the backend on port 5000, so no extra wiring is needed.

To stop both, press `Ctrl+C` once in the terminal running `python app.py` —
it shuts the frontend child process down along with the backend.

### Running them separately (optional)

If you'd rather run each in its own terminal (e.g. for frontend HMR logs),
set `LAUNCH_FRONTEND=false` before running `python app.py`, then in a
second terminal:

```bash
cd frontend
npm run dev
```

### Changing ports

```bash
# backend/.env or shell env vars, before `python app.py`
BACKEND_PORT=5000
FRONTEND_PORT=5173
FRONTEND_DIR=../frontend   # only needed if you move the folders apart
```

If you change `BACKEND_PORT`, also update the proxy `target` in
`frontend/vite.config.js` to match.

## What changed from the old single-port setup

The backend used to serve a static `static/index.html` chat UI directly at
`http://localhost:5000/`. That's been replaced by the React app in
`frontend/`, which now runs as its own process on its own port (5173).
Hitting `http://localhost:5000/` now just redirects to the React app.
The `/api/chat` contract (request/response shape) is unchanged, so the new
frontend talks to the exact same backend logic as before.
