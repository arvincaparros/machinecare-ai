# MachineCare AI — Enterprise Architecture Assessment

> Status: planning document. No application code, migrations, tests, dependencies or
> configuration were changed to produce it.
>
> Legend used throughout the plan set:
> **[VERIFIED]** inspected in the repository at the time of writing ·
> **[RECOMMENDATION]** proposed design, not yet implemented ·
> **[ASSUMPTION]** believed true but not provable from the repository ·
> **[OPEN]** requires a human decision.

---

## 1. Executive summary

MachineCare AI is a **local-first, evidence-grounded RAG assistant** for maintenance
personnel. A React 19 chat UI talks to a FastAPI RAG service that retrieves historical
closed downtime incidents from ChromaDB, revalidates every candidate against PostgreSQL,
and asks Groq to answer *only* from that evidence, with citation enforcement and
disclosures for unsupported claims.

The engineering quality of the **answering and ingestion core is high for a prototype**:
the grounding prompt is unusually disciplined, ChromaDB is correctly treated as a
candidate index rather than a source of truth, the import pipeline has digest-bound
previews and single-transaction commits with explicit recovery semantics, and the
backend test suite is substantial (**412 tests collected, 409 pass, 3 skipped**).

The **platform** around that core is not enterprise-ready. There is no authentication, no
authorization, no multi-user isolation, no persistence of conversations, no audit trail,
no CI/CD, no production container build, and no automated frontend tests. The incident
import is an **unauthenticated write path into PostgreSQL**.

The recommended evolution is a **modular monolith on the existing stack** — not
microservices. The natural seam is already present: the batch ingestion/administration
surface is operationally different from the low-latency RAG read path, and the LLM call
dominates request latency, but that does not justify splitting the process. Every target
capability in scope (identity, conversations, incident CRUD, streaming, audit) can be
delivered as new routers, services, models and migrations inside the existing
`backend/app` package and the existing React application.

This document is the assessment that the five plans in this directory build on:

| Document | Scope |
| --- | --- |
| `authentication-and-rbac-plan.md` | Phase 1 — identity, roles, endpoint protection |
| `conversation-management-and-persistence-plan.md` | Phases 2–3 — conversations, messages, multi-turn RAG |
| `incident-management-and-rag-evolution-plan.md` | Phases 4 and 7 — incident CRUD, vector sync, retrieval quality |
| `production-readiness-plan.md` | Phases 5–6 — streaming, Docker, CI/CD, operations |
| `implementation-roadmap.md` | Ordering, branches, migrations, risks, milestones |

---

## 2. Current architecture

### 2.1 Verified component map

```
Browser (React 19 + Vite dev server :5173)
  |  same-origin /api/v1/ask, /api/v1/ask-with-files, /health
  |  same-origin /api/v1/incidents/import/*
  |  Vite dev proxy -> http://127.0.0.1:8001 (vite.config.ts)
  v
RAG API  backend/app/api/main.py  (FastAPI, uvicorn :8001)
  |-- app.api.incident_import router  /api/v1/incidents/import/*
  |-- app.services.document_extraction  in-memory PDF/TXT/CSV/DOCX/XLSX/XLS
  v
app.services.rag_service.ask_machinecare / ask_machinecare_with_documents
  |-- vector_store_service.search_similar_incidents  (ChromaDB cosine, top-5, optional machine filter)
  |-- distance gate MAX_DISTANCE = 0.50
  |-- _collect_evidence: reload each ID from PostgreSQL, keep Closed + machine + resolution
  |-- groq_service.generate_response  (Groq chat completions, temperature 0.2)
  |-- validate_citations / find_unavailable_document_references
  v
PostgreSQL 17 (compose.yaml, host port 127.0.0.1:5434)
  machines, downtime_incidents, downtime_resolutions   (Alembic revision 6041a26eff31)

ChromaDB persistent client at backend/data/chroma_db
  collection "machine_downtime_incidents", cosine, DefaultEmbeddingFunction (ONNX MiniLM)
```

### 2.2 Verified entry points

| Surface | Module / symbol | Port | Used by UI | Auth |
| --- | --- | --- | --- | --- |
| RAG API app | `backend/app/api/main.py: app` | 8001 (`--port 8001`) | yes | none |
| Import router | `backend/app/api/incident_import.py: router` → `main.app.include_router` (`main.py:102`) | 8001 | yes | none |
| Phase 1 app | `backend/app/main.py: app` | 8000 (uvicorn default) | no | none |

### 2.3 Verified data model (PostgreSQL, Alembic `6041a26eff31`)

| Table | Columns | Constraints |
| --- | --- | --- |
| `machines` | `id`, `machine_code` (100), `name` (150), `production_line` (100, null) | PK `id`, unique `machine_code` |
| `downtime_incidents` | `id`, `incident_id` (50), `machine_id`, `production_line` (100, null), `date`, `product` (150, null), `issue_category` (100, null), `cause` (text), `downtime_minutes`, `responsible_team` (100, null), `status` (50) | PK `id`, unique `incident_id`, FK `machine_id → machines.id` |
| `downtime_resolutions` | `id`, `incident_id`, `root_cause` (text, null), `action_taken` (text, null), `preventive_action` (text, null) | PK `id`, unique `incident_id`, FK `incident_id → downtime_incidents.id` |

There are **no timestamps, no audit columns, no soft-delete columns, and no users table**.
There is no index on `downtime_incidents.status` (every retrieval revalidation filters on it)
and none on `downtime_incidents.machine_id` (FK lookups).

### 2.4 Verified request lifecycle properties

- **Lazy imports are load-bearing.** `app/db/database.py` reads `DB_USER`, `DB_PASSWORD`,
  `DB_NAME` from the environment **at import time** (`database.py:16-21`) and raises
  `KeyError` when missing. For that reason `app/api/main.py:62` and
  `app/api/incident_import.py` defer imports of the database, models, repositories and vector
  store until a request needs them. `/health` therefore answers without a database.
- **The vector store opens at import time.** `vector_store_service.py:11-24` constructs a
  `PersistentClient` and calls `get_or_create_collection` at module import.
- **The repository/import session is synchronous.** All endpoints are `def` (threadpool)
  handlers using a synchronous SQLAlchemy `Session` (`get_db` in `database.py:31-38`).
- **Generation is non-streaming.** `groq_service.generate_response` returns a complete string.
- **Frontend state is in-memory only.** `useAsk.ts` holds `turns` in `useState`; there is no
  router, no persistence, no auth context, and no global state store (`App.tsx` composes
  `useAsk`, `useAttachments`, `useFileDrop`, `useApiHealth`).

---

## 3. Existing capabilities (verified)

### 3.1 RAG answering pipeline — `backend/app/services/rag_service.py`

| Capability | Evidence |
| --- | --- |
| Dense retrieval, top-5 | `ask_machinecare` → `search_similar_incidents(question, top_k=5, machine)` (`rag_service.py:308-312`) |
| Distance gate 0.50 | `MAX_DISTANCE = 0.50` (`rag_service.py:18`), filter at `rag_service.py:315-319` |
| Exact machine filter | passed to Chroma metadata filter and re-checked in `_collect_evidence` (`rag_service.py:140`) |
| Chroma as candidate index only | `_collect_evidence` reloads each ID via `get_incident_by_number` (`rag_service.py:110-151`) |
| Drop missing / non-Closed / resolution-less / machine-mismatch | `rag_service.py:127-141` |
| No evidence ⇒ no LLM call | `rag_service.py:332-341` returns the fixed fallback |
| Grounding prompt (11 rules) | `build_prompt` (`rag_service.py:154-231`) |
| Citation extraction and validation | `extract_incident_ids`, `validate_citations` (`rag_service.py:81-89`, `234-264`), normalization of dash variants and zero-width chars (`rag_service.py:23-29`) |
| Unsupported-citation disclosure | `rag_service.py:352-359` |
| Unindexed-document disclosure | `find_unavailable_document_references` (`rag_service.py:267-294`), applied `rag_service.py:362-374` |

### 3.2 Document attachments — `backend/app/services/document_extraction.py`

| Protection | Evidence |
| --- | --- |
| Formats PDF/TXT/CSV/DOCX/XLSX/XLS | extension allow-list enforced before parsing |
| Max 3 files, 10 MB each | `MAX_FILES`, `MAX_FILE_BYTES`; read in 64 KB chunks with early 413 (`api/main.py:138-167`) |
| Signature validation, zip-bomb caps | signature checks per format; XLSX/DOCX entry-count and expanded-size caps |
| In-memory, never persisted | extraction returns text; nothing written to disk/DB/Chroma |
| Prompt-injection defence | `neutralize_document_instructions` (`rag_service.py:431-452`), untrusted markers (`_format_documents`, `rag_service.py:455-479`) |
| Shared prompt budget 12,000 chars | `MAX_DOCUMENT_PROMPT_CHARS` (`rag_service.py:394`) |

### 3.3 Incident import — `backend/app/api/incident_import.py` + `app/services/incident_import_*.py`

| Capability | Evidence |
| --- | --- |
| 5 MB / 5,000 rows / 20,000 scanned rows | `incident_import_service.py:39-43` |
| Read-only preview with row-level errors | `preview_import` (`incident_import.py`), `build_import_preview` |
| Digest-bound commit | `sha256_hex`, `digest_matches` (constant-time) — `incident_import_digest.py` |
| Single transaction, re-check inside transaction | `commit_incident_import` (`incident_import_commit.py`) |
| 409 on stale data | `IncidentImportStaleData` → `_rejection(409, …)` |
| Index after commit, reported separately | `_index_after_commit` — never decides whether rows are kept |
| Independent reindex retry | `POST /reindex`, upserts all Closed incidents, never deletes |
| Ambiguous-timeout policy | frontend `useIncidentImport.ts` never auto-retries a commit |

### 3.4 Frontend — `frontend/src`

| Capability | Files |
| --- | --- |
| Chat thread, Markdown/GFM, sources rail, retry | `App.tsx`, `ChatThread.tsx`, `AnswerCard.tsx`, `Markdown.tsx`, `SourcesPanel.tsx` |
| Composer with attachments, 2,000-char limit | `Composer.tsx`, `useAttachments.ts`, `lib/files.ts` |
| Drag-and-drop with overlay | `useFileDrop.ts`, `DropOverlay.tsx` |
| Health pill with 30 s polling | `useApiHealth.ts`, `StatusPill.tsx` |
| Import modal (809 lines) with its own state machine | `ImportDialog.tsx`, `hooks/useIncidentImport.ts`, `api/importClient.ts`, `lib/importFile.ts` |

### 3.5 Testing — `backend/tests`

Verified collection: **412 tests collected**; README reports **409 passed, 3 skipped**. The
three skipped are `tests/test_import_concurrency_postgres.py` (need `IMPORT_TEST_POSTGRES_URL`).

| Test module | Tests | Notes |
| --- | --- | --- |
| `test_incident_import_service.py` | 49 | parser/validation |
| `test_api_incident_import.py` | 37 | routes |
| `test_excel_extraction.py` | 35 | xlsx/xls |
| `test_api_incident_import_commit.py` | 30 | commit routes |
| `test_document_extraction.py` | 30 | attachments |
| `test_api_ask_with_files.py` | 27 | `/ask-with-files` |
| `test_rag_grounding.py` | 24 | grounding/citations |
| `test_retrieval_eval.py` / `test_rerank_eval.py` | 20 / 17 | evaluation harness |
| `test_rag_documents.py` | 17 | document-aware flow |
| `test_incident_import_preview.py` | 16 | preview classification |
| `test_api.py` | 15 | ask routes |
| `test_incident_import_commit.py` | 13 | commit service |
| `test_rag_freshness.py` | 7 | Chroma staleness |
| `test_incident_import_template.py` | 7 | templates |
| `test_incident_import_digest.py` | 5 | digest |
| `test_import_concurrency_postgres.py` | 3 | **skipped** without Postgres |

The suite uses in-memory SQLite (`tests/conftest.py`) with `StaticPool` + savepoint
isolation, swaps `rag_service.search_similar_incidents` and `rag_service.generate_response`
via `monkeypatch`, and overrides FastAPI dependencies (`get_ask_service`,
`get_document_ask_service`, `get_index_service`, `get_db_session`).

---

## 4. Verified gaps vs. outdated observations

Each reported gap from the brief was checked against the code.

| # | Reported gap | Status | Evidence |
| --- | --- | --- | --- |
| 1 | No authentication | **VERIFIED** | no auth dependency anywhere; `incident_import.py` module docstring states this explicitly |
| 2 | No RBAC | **VERIFIED** | no users/roles model or table |
| 3 | Import endpoints unauthenticated | **VERIFIED** | `POST /api/v1/incidents/import`, `/preview`, `/reindex` have no dependency |
| 4 | No audit logging | **VERIFIED** | only `logging.exception` for failures (`api/main.py:131`, `incident_import.py`) |
| 5 | No incident update/delete | **VERIFIED** | no PUT/PATCH/DELETE routes; `commit_incident_import` only inserts |
| 6 | No persistent history | **VERIFIED** | `useAsk.ts` `useState` only |
| 7 | No conversation ownership | **VERIFIED** | no conversation concept exists |
| 8 | No restoration after refresh | **VERIFIED** | no localStorage/sessionStorage usage; README §9 "A page refresh clears the thread" |
| 9 | No multi-turn context | **VERIFIED** | each request sends only its own question; `useAsk.ask` never includes prior turns |
| 10 | No title management | **VERIFIED** | no conversation entity |
| 11 | No message persistence | **VERIFIED** | no messages table |
| 12 | No streaming | **VERIFIED** | `generate_response` returns a full string; no `StreamingResponse`/SSE |
| 13 | No cancellation | **PARTIALLY VERIFIED** | `useAsk` has an `AbortController` (`useAsk.ts:38,55`) that aborts on unmount/replacement, but there is **no user-facing cancel control**, and the server does not stop the Groq call |
| 14 | No conversation sidebar | **VERIFIED** | `App.tsx` layout is hero + panel + rail |
| 15 | No persistent chat state | **VERIFIED** | same as #8 |
| 16 | Dense retrieval only | **VERIFIED** | production path is Chroma cosine only; lexical helper exists but is unused by production (see §7) |
| 17 | Hybrid evaluation limited | **VERIFIED** | `tests/rerank_eval.py` is an experiment; its own docstring: "nothing in app/ imports this module" |
| 18 | No validated held-out set | **VERIFIED** | `tests/retrieval_eval_questions.json` contains **13 questions**; sweep params are tuned on the same set |
| 19 | No OCR | **VERIFIED** | `document_extraction.py` rejects PDFs with no extractable text; README §10 |
| 20 | Threshold lacks calibration | **VERIFIED with nuance** | `tests/retrieval_eval.py` **does** sweep thresholds (0.45–0.70) and reports retention/acceptance, but there is no documented decision record or independent set |
| 21 | No CI/CD | **VERIFIED** | no `.github/` directory |
| 22 | Incomplete Docker | **VERIFIED** | `compose.yaml` contains **only** PostgreSQL; no Dockerfiles |
| 23 | No production frontend serving | **VERIFIED** | Vite dev proxy only; no nginx/Caddy/static serving config |
| 24 | Manual vector sync | **VERIFIED** | `index_incidents.py` is a script; only the import's post-commit step calls `index_incidents()` automatically |
| 25 | No frontend tests | **VERIFIED** | `package.json` has no test script; no runner dependency |
| 26 | Legacy/duplicate surfaces | **VERIFIED** | `backend/app/main.py` is a self-contained SQLite/`DATABASE_URL` app with its own `incidents` table; see §7 |

**Additional verified findings not in the brief:**

- **No timestamps on incident tables** — blocks "who changed what, when" and "sort by recency".
- **No FK `ON DELETE` policy declared** — `downtime_resolutions.incident_id` has no
  `ondelete`; deleting an incident row today would fail or orphan depending on DB config.
- **No index on `downtime_incidents.status` or `machine_id`.**
- **`get_db()` does not roll back on exception** (`database.py:31-38`); long-lived
  `SessionLocal` sessions in error paths rely on `commit_incident_import`'s explicit
  `rollback`. New write services must not inherit a weaker assumption.
- **Machine filter is exact-match free text** with no endpoint that powers autocomplete;
  `GET /api/v1/incidents/import/machines` exists but is only used by the import dialog.
- **`/health` is liveness only** — it does not check PostgreSQL, ChromaDB or Groq.
- **A failed index after commit leaves a stale vector store** which is masked by
  `_collect_evidence` revalidation but not repaired until a manual/retry reindex.
- **Fernet/Argon2/JWT libraries are not installed** (`requirements.txt` has none).

---

## 5. Technical debt

| ID | Debt | Location | Impact | Severity |
| --- | --- | --- | --- | --- |
| TD-1 | **Two independent data surfaces** — Alembic/PostgreSQL schema vs. Phase 1 SQLite `incidents` table | `backend/app/main.py`, `backend/machinecare.db` | confusion, two vocabularies, accidental edits | Medium |
| TD-2 | **Import-time configuration coupling** — `DB_*` required at import of `app.db.database`; vector store opens at import | `db/database.py:16`, `vector_store_service.py:11` | prevents clean settings injection, complicates tests/containers | Medium |
| TD-3 | **No settings object** — `os.environ` reads scattered across `database.py`, `groq_service.py` | both | hard to validate and document config | Medium |
| TD-4 | **Duplicated constants** between Python and TypeScript (`types.ts` mirrors key limits) | `types.ts`, `api/schemas.py`, `incident_import_schemas.py` | drift risk | Low |
| TD-5 | **Unused production-code helper** `search_incidents` in `incident_retrieval_service.py` used only by a manual smoke script | `services/incident_retrieval_service.py` | dead path; confuses hybrid-eval story | Low |
| TD-6 | **`ImportDialog.tsx` is 809 lines** with logic split between component and `useIncidentImport.ts` | `components/ImportDialog.tsx` | untested, hard to change | Medium |
| TD-7 | **No type generation from OpenAPI** — frontend types are hand-maintained | `types.ts` header comment | drift risk | Low |
| TD-8 | **Synchronous DB + blocking LLM call inside a sync endpoint** | `api/main.py` | threadpool exhaustion under load; also blocks streaming | Medium (Phase 5) |
| TD-9 | **No index on `status`/`machine_id`** | migration `6041a26eff31` | revalidation query cost grows | Low→Medium at scale |
| TD-10 | **`get_db` lacks rollback on error** | `db/database.py:31-38` | new writers could leak partial state if they forget explicit rollback | Low today |
| TD-11 | **No `.env.example` committed** though `.gitignore` whitelists it | repo root | setup friction | Low |
| TD-12 | **README is the only architecture doc** (very good, but a single point of truth) | `README.md` | maintenance | Low |

---

## 6. Security risks

Ordered by risk. Each is scoped to what actually exists in the code.

| ID | Risk | Evidence | Impact | Mitigation (plan reference) |
| --- | --- | --- | --- | --- |
| SR-1 | **Unauthenticated write path** (`POST /api/v1/incidents/import`, `/reindex`) | `incident_import.py` router has no dependencies | anyone who can reach :8001 can inject incidents and trigger full reindex | Phase 1 auth + role `can_import` |
| SR-2 | **No user isolation** (nothing to isolate yet, but no primitive to build on) | no users table | every conversation/RAG call is anonymous | Phase 1 users; Phase 2 ownership |
| SR-3 | **No incident mutation controls** | only insert exists; editing is manual SQL | no accountability for data changes | Phase 4 CRUD with authz + audit |
| SR-4 | **No audit trail** | failure-only logs | cannot answer "who changed this record" | Phase 6 audit log (built with Phase 1) |
| SR-5 | **No rate limiting / abuse controls on cost-incurring endpoints** | `/ask` calls a paid LLM | cost abuse, DoS by expensive prompts | Phase 6 (documented as `[OPEN]` for infra choice) |
| SR-6 | **No TLS** | README §14 | credentials/tokens in clear text if ever networked | Phase 7 reverse proxy |
| SR-7 | **LLM egress of incident data** | `groq_service.py` | confidentiality | data-handling policy + optional provider abstraction |
| SR-8 | **No secrets management beyond a git-ignored `.env`** | README §5 | key sprawl | Phase 7 secrets plan |
| SR-9 | **Health endpoint claims only liveness** | `api/main.py:105-113` | misleading ops signal | Phase 7 readiness endpoint |

**Strengths to preserve (do not regress):** attachment extraction is in-memory with
signature and zip-bomb caps; prompt-injection neutralization; error responses are generic
while tracebacks stay server-side; `hmac.compare_digest` for the import digest; the
`_collect_evidence` revalidation that makes stale vectors harmless.

---

## 7. Legacy code findings

### 7.1 `backend/app/main.py` — Phase 1 database-only app

- **[VERIFIED]** Self-contained: defines its own `Base`, `Incident` model on table
  `incidents` with flat columns (`id` as business string PK), its own `IncidentOut`
  schema, and a lifespan that runs `Base.metadata.create_all(engine)` and seeds from
  `backend/data/incidents.json`.
- **[VERIFIED]** Not imported by `app/api/main.py`, not imported by any test, not called by
  the frontend.
- **[VERIFIED]** Defaults to SQLite `backend/machinecare.db`; `.gitignore` ignores `*.db`.
- **[VERIFIED]** Uses `DATABASE_URL` if set, independent of `DB_*`.
- **[VERIFIED]** If started against the RAG PostgreSQL database, `Base.metadata.create_all`
  would attempt to create the `incidents` table in the same database (a *third* schema).
- **[RECOMMENDATION]** Do **not** delete or rename. Mark it deprecated in README, ensure CI
  never starts it, and remove it in a dedicated, approved cleanup branch after Phase 7.
  Track as TD-1.

### 7.2 `backend/app/services/incident_retrieval_service.py`

- **[VERIFIED]** `load_incidents()` is used by `seed_database.py` (production-intended).
- **[VERIFIED]** `search_incidents()` (naive keyword overlap over JSON) is used only by
  the manual smoke script `backend/test_retrieval.py`.
- **[RECOMMENDATION]** Keep `load_incidents`. Treat `search_incidents` as a candidate
  reference implementation for a future BM25 layer, but do not present it as production
  retrieval. Low priority.

### 7.3 Root-level manual scripts

- **[VERIFIED]** `backend/test_*.py` (8 files) are smoke scripts, not collected by pytest
  (`pytest.ini`: `testpaths = tests`).
- **[RECOMMENDATION]** Keep for now; move under a `scripts/` directory only with approval,
  because their paths are documented in README §13.

### 7.4 Duplicated test-visible seams

- **[VERIFIED]** `app.services.incident_import_digest` was deliberately split out to avoid
  a DB import in tests. Any refactor must preserve module-level import boundaries; the API
  layer relies on lazy imports so `/health` works without DB settings. This is a
  **constraint** on all new code, especially new dependency-injected auth settings.

---

## 8. Scalability considerations

### 8.1 Verified constraints

| Constraint | Why it matters |
| --- | --- |
| Sync SQLAlchemy + def endpoints | concurrency is limited by FastAPI's threadpool; fine for tens of users |
| LLM call is blocking and the slowest step | one in-flight question per browser, but N browsers share the threadpool |
| ChromaDB persistent client is opened per process | **not multi-process safe for concurrent writes**; multi-worker uvicorn + import/reindex races are a real risk |
| Full reindex upserts every Closed incident | O(N) per import; acceptable at 100 records, linear growth afterwards |
| No pagination on incident import machines list | unbounded response |
| No pagination on `/incidents` in Phase 1 (limit 1–100) | not used by UI |

### 8.2 Recommendations

- **[RECOMMENDATION]** Keep a **single uvicorn process** for the API until ChromaDB is
  replaced or moved behind a service. If multi-worker is needed, make indexing an
  exclusive job (advisory lock in PostgreSQL, or a job row) — Phase 4 design.
- **[RECOMMENDATION]** Move long-running indexing off the request thread before
  horizontal scaling (Phase 4a: index job record + worker execution).
- **[RECOMMENDATION]** Add pagination to conversation listing and incident listing in the
  same phase that introduces the tables (cursor by `updated_at, id`).
- **[RECOMMENDATION]** Do **not** adopt microservices. The modular monolith keeps the
  grounding pipeline atomic and testable; the only external dependency worth isolating
  later is the LLM provider (already one module).

---

## 9. Architecture recommendations

### 9.1 Target shape (modular monolith)

```
backend/app
├─ api/            routers only: auth, conversations, incidents, import, ask, admin, audit, health
├─ core/           settings (pydantic-settings), security (Argon2/JWT), errors, pagination
├─ middleware/     request-id + audit context, security headers, (Phase 5) rate limit
├─ models/         user, role, conversation, message, audit, index_job + existing three
├─ repositories/   one module per aggregate, query-only
├─ schemas/        split from api/ (new modules only; existing api/*_schemas.py untouched)
├─ services/       auth, conversation, rag (existing), index_job, audit, import (existing)
└─ dependencies/   current_user, require_permission, get_db wrapper
```

**[RECOMMENDATION]** Do not move existing modules during Phases 1–4. Add new ones beside
them and keep `app/api/main.py` as the single composable app so every existing test that
imports `from app.api.main import app` keeps working. This is the lowest-risk route and
preserves all 412 existing tests unchanged.

### 9.2 Cross-cutting decisions

| Decision | Recommendation | Rationale |
| --- | --- | --- |
| Identity store | PostgreSQL tables in the same DB | one backup story; no new infra |
| Token model | Short-lived JWT access token in memory + opaque refresh token in an HttpOnly cookie, hashed at rest | limits XSS and revocation gaps |
| Authorization | Permission codes checked by FastAPI dependencies, roles map to permission sets | not route-name based; testable |
| Conversations | `conversations` + `conversation_messages`, JSONB for citations/retrieval metadata | matches existing JSON response shapes |
| Audit | append-only `audit_log` written in the same transaction as the mutation | deletion/update accountability |
| Vector sync | index job rows + idempotent upsert + delete-by-ID; never dual-write without recovery | existing commit-then-index pattern generalised |
| Streaming | SSE over `POST`+`fetch` reader (not native EventSource) | needs POST body + auth headers; simplest reliable |
| Retrieval quality | Add BM25 + RRF fusion behind a feature flag, promote only after held-out eval | preserves grounding guarantees |
| Frontend | Add React Router + TanStack Query; split `App.tsx` into layout + pages | minimal new dependencies |

### 9.3 Required new dependencies (for approval — not installed)

| Package | Purpose | Phase |
| --- | --- | --- |
| `pwdlib[argon2]` (or `argon2-cffi`) | Argon2id hashing | 1 |
| `pyjwt[crypto]` | JWT access tokens | 1 |
| `pydantic-settings` | typed settings replacing scattered `os.environ` | 1 |
| `rank-bm25` or stdlib BM25 | hybrid retrieval | 7 |
| `pytesseract` + system Tesseract, or `ocrmypdf` | OCR | 7 |
| `ruff`, `mypy` (dev) | lint/type gate | 6 |
| `vitest`, `@testing-library/react`, `@testing-library/user-event`, `jsdom`, `msw` (dev) | frontend tests | 6 |
| `react-router-dom`, `@tanstack/react-query` | shell, data fetching | 2 |
| `slowapi` or proxy-level limits | rate limiting | 6 |

All are **[OPEN]** pending approval; the plan documents versions to be pinned at
implementation time.

---

## 10. Architecture risks introduced by the plan

| Risk | Phase | Mitigation |
| --- | --- | --- |
| Protecting `/api/v1/ask` breaks all existing API tests that call it unauthenticated | 1 | Add auth fixtures and overrides; **do not** make auth optional in production. Existing tests asserting anonymous behavior are **expected to be updated by adding an auth fixture**, not by weakening assertions. Explicitly called out in each phase's acceptance criteria. |
| Deleting incidents while Chroma holds them | 4 | `_collect_evidence` already drops missing IDs; add vector delete + index job |
| Persisting messages that contain unsupported citations as "truth" | 2–3 | store citation flags verbatim; never feed stored assistant text back as evidence |
| Streaming changes response shape | 5 | keep `/api/v1/ask` unchanged; add `/api/v1/ask/stream` (or conversation-scoped equivalent) |
| Multi-worker Chroma corruption | 4/6 | single process or exclusive index job |
| Phase 2 schema limits future streaming | 2 | message status enum includes `streaming`/`cancelled`/`failed` from day one |

---

## 11. Open decisions requiring human approval

1. **Registration policy**: admin-provisioned only vs. open registration with approval
   queue. Plan assumes **admin-provisioned** (`[ASSUMPTION]`).
2. **Role granularity**: three fixed roles (Admin / Maintenance Engineer / Viewer) vs.
   permission sets stored per role. Plan recommends three roles mapped to **permission
   codes** internally.
3. **Maintenance Engineer import rights**: the brief says "if permitted". Plan offers a
   permission `incidents:import` granted to Engineer by default (`[OPEN]`).
4. **Retention** of conversations and audit logs (proposed: conversations soft-deleted,
   audit retained indefinitely until a policy exists).
5. **LLM provider abstraction**: stay Groq-only vs. add an interface for future
   self-hosting. Plan recommends a thin provider seam in Phase 7 only.
6. **Production deployment target** (single VM + Compose vs. managed services).
   Plan recommends single VM + Compose + reverse proxy until scale demands otherwise.
7. **Whether Phase 1 app is retired** in a dedicated cleanup branch.

---

## 12. Definition of "enterprise-ready" for this repository

A release satisfies this assessment when:

- Every endpoint except `/health` (and `POST /api/v1/auth/login`) requires an
  authenticated principal; every mutation checks a permission server-side.
- All conversation and message reads/writes are scoped by `user_id` at the query level.
- Every incident mutation and authentication event is auditable.
- Vector index consistency is recoverable from PostgreSQL alone (proven by test).
- CI runs lint, type checks, backend tests, frontend tests, migration validation and the
  retrieval evaluation on every pull request.
- The grounding guarantees of `rag_service.py` are unchanged and regression-tested.