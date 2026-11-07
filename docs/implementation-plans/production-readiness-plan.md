# MachineCare AI — Production Readiness Plan (Phases 5–6)

> Planning only. Covers streaming (Phase 5), then containerisation, CI/CD, frontend testing,
> deployment, monitoring, backup and production security (Phase 6). See
> `enterprise-architecture-assessment.md` for the legend.

---

## Part A — Streaming AI responses (Phase 5)

### 1. Current state (verified)

| Fact | Location |
| --- | --- |
| Generation returns a complete string | `groq_service.generate_response` |
| Ask endpoints are synchronous `def` JSON handlers | `app/api/main.py:116,170` |
| Client uses `fetch` + 90 s / 120 s timeouts | `api/client.ts` |
| `AbortController` aborts the browser request, not the server call | `useAsk.ts:55` |
| Message status model already supports `streaming`/`cancelled` | Phase 2 schema |

### 2. SSE vs WebSocket

| Criterion | SSE (POST + fetch stream) | WebSocket |
| --- | --- | --- |
| Direction | server → client only (sufficient) | bidirectional (unused here) |
| Auth headers | sendable with `fetch` | needs token in URL/query or subprotocol |
| Reconnection | native with `EventSource` (but GET-only, no body) | manual |
| Proxy/infra | plain HTTP chunked | needs upgrade support |
| Cancellation | client aborts fetch; server detects disconnect | explicit message |
| Complexity | low | higher |

**Recommendation: SSE delivered over a POST request read with `fetch` + `ReadableStream`.
Do not use native `EventSource`** (it cannot send an Authorization header or a POST body).
WebSockets are unnecessary because nothing is client-initiated mid-stream.

### 3. Design

New endpoint (conversation-scoped; Phase 2 must land first):

```
POST /api/v1/conversations/{id}/messages/stream   (multipart or JSON)
  -> text/event-stream
     event: status  data: {message_id, state:"retrieving"}
     event: token   data: {delta:"..."}
     event: status  data: {message_id, state:"verifying"}
     event: final   data: {message: Message}      // persisted, citations validated
     event: error   data: {message_id, detail:"generic"}
```

Rules:

1. **Persist before streaming**: assistant row `status='streaming'` committed before the LLM
   call, so a disconnect has a record to repair.
2. **Citations are validated only on completion.** During token streaming the UI must show
   a "verifying citations" state; the `final` event carries the validated message. This
   preserves the existing guarantee that unsupported IDs are flagged before display.
3. **Cancellation**: client `AbortController` closes the connection; server detects
   disconnect (or an explicit `POST .../messages/{id}/cancel`), stops consuming the Groq
   stream, and persists `status='cancelled'` with partial content and a metadata flag.
4. **Failure**: persist `status='failed'` and emit `event: error`. Partial text is never
   emitted as `final`.
5. **Network disconnection on the client**: show "Answer interrupted", keep the partial text
   clearly labelled as incomplete, offer Retry (new message, not overwrite).
6. **Future compatibility**: stateless `/api/v1/ask` remains non-streaming; the streaming
   endpoint is additive.
7. **No-evidence path streams a single `final` event** with the fixed fallback; no LLM call.

### 4. Backend implementation constraints

- FastAPI streaming requires an **async generator** endpoint; the existing DB and Groq
  clients are synchronous. Use `run_in_threadpool` for DB writes and a **thread-safe bridge**
  for the Groq iterator, or use the Groq **async** client for the streaming call.
- Do not hold a DB session open for the whole stream (§Phase 2 §4.5). Open short sessions
  around persistence points.
- Disable proxy buffering (`X-Accel-Buffering: no` for nginx).
- Set SSE heartbeat comments every 15 s to keep proxies from closing idle connections.

### 5. Frontend changes

| File | Action |
| --- | --- |
| `src/api/conversationClient.ts` | **modify**: `streamMessage()` parsing SSE frames from `response.body` |
| `src/hooks/useConversation.ts` | **modify**: append deltas to a transient message, replace with `final` |
| `src/components/ChatThread.tsx` | **modify**: streaming indicator, cancel button |
| `src/components/AnswerCard.tsx` | **modify**: "incomplete/cancelled" styling |

### 6. Streaming acceptance criteria

- [ ] Tokens appear progressively; citations appear only after validation.
- [ ] Cancel stops the server's Groq consumption and persists `cancelled`.
- [ ] A disconnect leaves a repairable message, never a `streaming` row past the timeout.
- [ ] An LLM error produces `failed` and a generic message; no partial text is shown as final.
- [ ] No evidence ⇒ single fallback final event, no LLM call.

---

## Part B — Containerisation

### 7. Current state (verified)

- `compose.yaml` runs **only** PostgreSQL 17 with a healthcheck and named volume.
- No `Dockerfile` for backend or frontend; both run on the host in development.
- ChromaDB is a local directory (`backend/data/chroma_db`), git-ignored.

### 8. Proposed images

| Image | Base | Contents |
| --- | --- | --- |
| `backend` | `python:3.12-slim` | app, `requirements.txt`, non-root user, `uvicorn app.api.main:app --host 0.0.0.0 --port 8001` |
| `frontend` | multi-stage `node:22-alpine` → `nginx:alpine` | `npm ci && npm run build` → static assets + `nginx.conf` proxying `/api` and `/health` to the backend |

Key decisions:

- **Backend dependencies**: split `requirements.txt` (runtime) and `requirements-dev.txt`
  (already separate) — container installs runtime only.
- **[RECOMMENDATION]** Pin exact versions with a lock (`pip-compile`) before production.
  Currently ranges only (`fastapi>=0.115,<1`, etc.).
- **Chroma persistence**: mount a named volume at `/app/data/chroma_db`. The
  `vector_store_service` path is derived from `Path(__file__).resolve().parents[2]`, so it
  resolves inside the image; the volume must match that path.
- **Embedding model cache**: the ONNX MiniLM model downloads on first use; bake it into the
  image or mount a cache volume to avoid cold-start network dependency.
- **Single API process** (see assessment §8.2) until index jobs are safe across workers.
- **Healthchecks**: backend `curl -f http://localhost:8001/health`; frontend `wget -qO- http://localhost/`.
  Add a real readiness endpoint in Phase 7.
- **Gestures for OCR (if approved)**: install `tesseract-ocr` and `poppler-utils` in the
  image.

### 9. Compose topology (production-like)

```
services:
  postgres   (existing, keep host port unpublished in production)
  backend    depends_on postgres (healthy); volumes: chroma_data; env_file: .env
  frontend   depends_on backend; ports 80/443 behind reverse proxy
  reverse-proxy (Caddy/nginx): TLS termination, same-origin SPA + /api
```

**[RECOMMENDATION]** Same origin for SPA and API, required by the recommended auth
cookie design (`SameSite=Strict`).

---

## Part C — CI/CD

### 10. Current state (verified)

- No `.github/` directory, no workflows, no lint/type config for Python.
- `pytest.ini` sets `pythonpath = .`, `testpaths = tests`.
- Frontend `npm run build` runs `tsc --noEmit` first.

### 11. Proposed GitHub Actions

**Workflow `ci.yml` (pull_request, push to main)**

| Job | Steps |
| --- | --- |
| `backend-lint` | `ruff check backend`, `ruff format --check`, `mypy backend/app` |
| `backend-test` | install runtime+dev, `pytest -q` from `backend/` (412 tests) |
| `backend-migrations` | `alembic upgrade head` against a service Postgres, then `alembic check` (no autogenerate drift) |
| `retrieval-eval` | `python -m tests.retrieval_eval` on dev split; report artifact; fail on thresholds |
| `frontend` | `npm ci`, `npm run build` (tsc), `npm test -- --run` (Vitest) |
| `security` | dependency audit (`pip-audit`, `npm audit --omit=dev`), secret scan |

**Workflow `docker.yml` (tag/release)**: build and push backend/frontend images.

**Workflow `nightly.yml`**: full evaluation including heldout split and optional LLM answer
quality.

**[RECOMMENDATION]** The existing concurrency tests (`test_import_concurrency_postgres.py`)
run in `backend-migrations` job by setting `IMPORT_TEST_POSTGRES_URL` to the CI service
database named `machinecare_import_test`. This converts 3 skipped tests into executed ones.

### 12. Quality gates

| Gate | Threshold |
| --- | --- |
| Backend tests | all pass; no new skips without a linked justification |
| Frontend build + tests | all pass |
| Migration check | no un-autogenerated schema drift |
| Retrieval dev split | no regression vs the stored baseline |
| Coverage | informational in v1; set a floor after Phase 6 **[OPEN]** |

**Rule:** existing tests may be extended but **never weakened or deleted** to make a change
pass. Any test that must change semantics (e.g. auth) gets an explicit review note.

---

## Part D — Frontend testing

### 13. Current state (verified)

- No test script, no runner, no mocking library (`package.json`).
- `tsconfig.json` has `strict`, `noUnusedLocals`, `noUnusedParameters`.
- The import dialog is 809 lines and only hand-tested.

### 14. Proposed stack

| Package | Purpose |
| --- | --- |
| `vitest` | runner (Vite-native) |
| `@testing-library/react` + `@testing-library/user-event` | component tests |
| `jsdom` | DOM |
| `msw` | API mocking at the network layer |

### 15. Test plan

| Area | Tests |
| --- | --- |
| Auth | login success/failure; redirect when unauthenticated; permission-gated buttons |
| Chat | submit; markdown render; citations verified/unverified; retry on failure |
| Conversations | sidebar list; select; rename; delete confirm; restore after reload (MSW) |
| Import workflow | the full state machine: file selection → preview → commit; digest binding; `skip_existing` toggle clears preview; ambiguous failure blocks auto-retry; reindex retry |
| Attachments | validation errors; 3-file limit; drag-and-drop; metadata-only warning after reload |
| Streaming | incremental tokens; cancel; failed stream |

**[RECOMMENDATION]** Extract the import logic currently inside `ImportDialog.tsx` into the
existing `useIncidentImport.ts` hook before adding tests, so the state machine is testable
without rendering 809 lines of UI. This is a refactor with no behaviour change and must be
covered by the new tests.

---

## Part E — Deployment, operations and security

### 16. Deployment model

**[RECOMMENDATION]** Single VM (or single node) running Docker Compose with a reverse proxy
(Caddy for automatic TLS or nginx). Rationale: the system is a small internal tool; managed
Kubernetes or autoscaling would add cost and complexity with no demonstrated need.

| Concern | Recommendation |
| --- | --- |
| TLS | Caddy/nginx with HSTS; HTTP→HTTPS redirect |
| Process supervision | Compose `restart: unless-stopped` |
| Scaling | vertical first; single API instance until index jobs are separated |
| Zero-downtime | optional blue/green via Compose + proxy switch; not required initially |

### 17. Database backup and recovery

| Item | Recommendation |
| --- | --- |
| Backup | nightly `pg_dump` to off-host storage; retained 30 days **[OPEN]** |
| Restore drill | quarterly documented test restore |
| Chroma | **not backed up** — rebuildable by `reconcile()`/`index_incidents.py` from PostgreSQL |
| Migrations | forward-only in production; every migration has a tested downgrade |
| Data volume | `machinecare_pgdata`; `docker compose down -v` is destructive and must be documented as such |

**[RECOMMENDATION]** Chroma being derived data is a key operational simplification; the
restore procedure is "restore PostgreSQL → run reindex".

### 18. Secrets management

| Secret | Current | Recommendation |
| --- | --- | --- |
| `DB_PASSWORD` | `.env` | host `.env` with `chmod 600`; or Docker secrets / a password manager in production |
| `POSTGRES_PASSWORD` | `.env` | same |
| `GROQ_API_KEY` | `.env` | rotate on schedule; never in images or logs |
| `JWT_SECRET` | *new* | generated per environment; stored as a secret |

- Commit a `.env.example` (the `.gitignore` already whitelists it) listing every variable
  with **no values**.
- CI uses repository secrets; no secret is ever printed (masking + secret scan job).

### 19. Observability

| Signal | Implementation |
| --- | --- |
| Structured logs | JSON formatter with `request_id`, `user_id` (not username), route, status, duration |
| Request correlation | middleware generates `X-Request-ID`; propagated to audit rows |
| Error tracking | **[OPEN]** Sentry or equivalent; v1: structured error logs + alert on 5xx rate |
| Health | keep `/health` (liveness); add `/ready` checking DB (`SELECT 1`), Chroma collection count, and (optionally) Groq key presence |
| LLM latency | timed around `generate_response` |
| Retrieval latency | timed around `search_similar_incidents` |
| Token usage | capture provider usage into message `usage` JSONB and metrics |
| Index status | gauge from `index_jobs` by status; alert on `failed` > 0 or `queued` age > 10 min |
| Metrics endpoint | optional `/metrics` (Prometheus) **[OPEN]** |

**[RECOMMENDATION]** Do not log prompts, evidence, attachment text or tokens. Log counts and
latency only.

### 20. Production security checklist

- [ ] All endpoints except `/health`, `/ready`, `/api/v1/auth/login` require auth.
- [ ] Authorization enforced server-side for every mutation.
- [ ] Cookies `Secure`, `HttpOnly`, `SameSite=Strict`.
- [ ] Argon2id hashing; login rate-limited.
- [ ] No secrets in images, logs, error responses or audit metadata.
- [ ] Database not publicly reachable (internal network only).
- [ ] TLS with HSTS; security headers (`X-Content-Type-Options`, `Referrer-Policy`, CSP).
- [ ] Unauthenticated import/reindex impossible.
- [ ] Audit log covers auth, user admin, import, incident mutations, reindex.
- [ ] Backup and restore documented and tested.

### 21. Acceptance criteria (Phase 6)

- [ ] `docker compose up` (prod profile) starts postgres, backend, frontend, proxy; the UI
      loads over HTTPS and answers questions.
- [ ] CI is green on a clean clone with no local `.env` (tests use dummy `DB_*`, as today).
- [ ] Frontend Vitest suite runs in CI and covers the §15 areas.
- [ ] Retrieval evaluation artifact is produced on every PR.
- [ ] `/ready` reflects dependency health; alerts configured for 5xx rate and failed jobs.
- [ ] A documented restore drill rebuilds Chroma from PostgreSQL.

### 22. Open questions

1. Hosting target (single VM vs. managed) — plan assumes single VM.
2. Error-tracking vendor and metrics stack.
3. Retention for backups and logs.
4. Whether to add `pip-compile` lockfiles or pin manually.
5. OCR system packages in the production image (depends on Phase 7 approval).