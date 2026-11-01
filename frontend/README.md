# Frontend

Next.js 16 + TypeScript UI for the Infra Debugging Agent. It talks to the FastAPI backend.

Pages: the chat (live agent steps, then the answer and the RCA report card), `/jobs` (automatic investigations started by Jira, polling) and `/jobs/<id>` (detail), `/approvals` (review and approve or reject the agent's proposed Jira comments; needs an `admin` key).

```bash
npm install
npm run dev         # http://localhost:3000 (backend on http://127.0.0.1:8000)
npm test            # unit tests (vitest)
npm run typecheck   # next typegen && tsc --noEmit (Next's route types are generated, so typegen must run first)
npm run lint
npm run build       # production build (standalone output, used by the Docker image)
```

**API key:** the backend requires one. Paste it into the bar at the top of the page; it is kept in `sessionStorage` (gone when the tab closes) and sent as `Authorization: Bearer ...`. Create keys with `scripts/manage_api_keys.py` in the backend project.

`NEXT_PUBLIC_API_URL` points at a different backend. It is embedded into the browser bundle at **build** time (so it is a Docker build argument, not a runtime setting); never put secrets in any `NEXT_PUBLIC_` variable.

Layout: `src/app/` pages, `src/components/` (steps, report card, approval card, nav with the API key bar), `src/lib/` (`api.ts` backend calls with the auth header, `auth.ts` key storage, `sse.ts` Server-Sent Events parser for `fetch` streams, `types.ts` matching the backend schemas), `src/hooks/usePolling.ts`.
