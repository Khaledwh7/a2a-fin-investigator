# Deploy a live demo

The app is two processes — the **API** (FastAPI) and the **UI** (Streamlit). For a
free, one-click demo it can run as a **single service**: the UI boots the API
in-process when `EMBED_API` is set (see `ui/embed.py`).

## Option A — Streamlit Community Cloud (free, ~3 minutes) ✅ recommended

1. Push this repo to GitHub (already done).
2. Go to **https://share.streamlit.io** → sign in with GitHub → **Create app**.
3. Fill in:
   - **Repository:** `Khaledwh7/a2a-fin-investigator`
   - **Branch:** `main`
   - **Main file path:** `ui/app.py`
4. Open **Advanced settings**:
   - **Python version:** `3.12`
   - **Secrets** — paste this one line (this is what tells the UI to run the API in-process):
     ```toml
     EMBED_API = "1"
     ```
5. Click **Deploy**. First build takes a couple of minutes (it installs Streamlit + deps).
   When it's up you get a public URL like `https://<your-app>.streamlit.app`.
6. Put that URL in the README's **Live demo** line.

That's it — the UI starts the six A2A agents + REST gateway inside the same
container and talks to them over localhost, so everything works from one link.

> Notes: the demo uses SQLite on an **ephemeral** disk (case history resets when
> the app sleeps — fine for a demo). Human-in-the-loop review is **on**, so
> high-stakes cases pause for you to approve/override/close.

## Option B — Render / Railway / Fly.io (Docker)

Use the provided `Dockerfile`. Two clean ways:

- **One service (embedded):** run the UI container with `EMBED_API=1` and start
  command `streamlit run ui/app.py --server.port $PORT --server.address 0.0.0.0`.
- **Two services (closer to production):** deploy the **API**
  (`uvicorn app.main:app --host 0.0.0.0 --port $PORT`) and the **UI**
  (`streamlit run ui/app.py`) separately, and set the UI's `API_BASE_URL` to the
  API service's public URL. This mirrors the real split-service topology.

`docker compose up --build` also runs the full stack (API + UI + Postgres) locally.

## Run it locally (two terminals, no embedding)

```bash
uvicorn app.main:app --port 8000
```
```bash
API_BASE_URL=http://localhost:8000 streamlit run ui/app.py
```
