# Frontend

Chat UI for the Infra Debugging Agent (Next.js + TypeScript). It talks to the FastAPI backend and shows the agent's investigation live: every tool call as it finishes, then the answer and the RCA report card.

```bash
npm install
npm run dev     # http://localhost:3000 (backend must be running on http://127.0.0.1:8000)
npm test        # unit tests for the SSE parser
npm run build   # production build
```

Optional: set `NEXT_PUBLIC_API_URL` to point at a different backend. Anything with the `NEXT_PUBLIC_` prefix is embedded in the browser bundle, so never put secrets there.

Layout:

- `src/app/page.tsx` – the chat page (a Client Component)
- `src/components/` – `StepList` (investigation steps), `ReportCard` (RCA report)
- `src/lib/api.ts` – backend calls; `src/lib/sse.ts` – Server-Sent Events parser for `fetch` streams; `src/lib/types.ts` – types matching the backend schemas
