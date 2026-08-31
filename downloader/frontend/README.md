# Downloader frontend

React + TypeScript, built with Vite. This is the source for the UI served
by the FastAPI backend in `downloader/app` — see `docs/downloaderwebapparchitecture.md`
at the repo root for the overall system design.

## Development

```
npm install
npm run dev
```

`vite.config.ts` proxies the API routes to a real backend (set `BACKEND_URL`
if it's not at the default `http://192.168.68.4:8790`) so you get hot-reload
without a full Docker rebuild per change.

## Production build

Not run by hand — `docker compose build downloader` runs `npm ci && npm run
build` in a Node stage and copies the resulting `dist/` into
`downloader/app/web/`, which FastAPI serves directly (`app/main.py`). See
`downloader/Dockerfile`.
