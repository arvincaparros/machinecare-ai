# MachineCare AI

MachineCare AI answers maintenance questions from historical downtime incidents and cites the
records behind every claim. A React chat UI talks to a FastAPI retrieval-augmented generation
(RAG) service: PostgreSQL holds the authoritative incident records, ChromaDB narrows candidates
by semantic similarity, and Groq generates the answer from the retrieved evidence only.

This is a working local development stack, not a hardened production service. Sign-in with
role-based access control now guards every data endpoint (section 7), while the conversation
still lives only in the browser tab and attachments are per-question context rather than an
indexed document library. There is still no TLS or rate limiting, so keep the stack on a local
machine. See [section 15](#15-current-limitations-and-planned-features).

## 1. Project overview and current capabilities

What works today:

- **Grounded answers** over the 100 closed downtime incidents in `backend/data/incidents.json`
  (16 machine codes, all with resolutions).
- **Semantic retrieval** with ChromaDB cosine search (top 5) and a distance gate of `0.50`;
  every match is re-read from PostgreSQL before it can support an answer.
- **Citation enforcement**: incident IDs in the generated text are extracted, compared with the
  retrieved records, and unsupported IDs are reported in the response and flagged in the UI.
- **Optional machine filter** (exact machine code such as `AOI-01`) applied to both retrieval
  and the PostgreSQL reload.
- **Ad-hoc attachments** (PDF, TXT, CSV, DOCX, XLSX, XLS) used as extra context for a single
  question, chosen with the Attach button or dragged onto the chat panel.
- **Incident import** from a CSV or XLSX file, opened from the header: a read-only check with
  row-level errors first, then a single-transaction insert of the eligible rows, followed by a
  separate ChromaDB indexing step with a reindex retry (section 11).
- **Chat UI** with suggested prompts, follow-up questions, Markdown answers, a sources rail,
  error retry, and a liveness indicator.
- **Two FastAPI applications**: the RAG API the frontend uses, plus the standalone Phase 1
  database-only record API for inspecting the original incident table.
- **Accounts and roles**: administrator-provisioned sign-in with Argon2id password hashes,
  short-lived JWT access tokens and rotating HttpOnly refresh cookies (replaying a rotated-away
  cookie revokes the whole session family), three roles enforced by FastAPI dependencies on every
  data endpoint, login lockout, and an append-only audit log of security events.

What does **not** exist yet: incident update/delete endpoints, per-row import provenance, indexed
SOP/document management, self-service registration or email-based password reset, TLS and rate
limiting, MCP tools, streaming, and persisted chat history. Those are listed as planned work in
section 15.

## 2. Architecture and data flow

```
Browser (React 19 + Vite dev server, http://localhost:5173)
   |  same-origin /api/v1/ask, /api/v1/ask-with-files, /health
   |  same-origin /api/v1/incidents/import/* (template, machines, preview, commit, reindex)
   |  (Vite dev proxy forwards both to http://127.0.0.1:8001)
   v
RAG API (FastAPI, app.api.main:app, port 8001)
   |  1. validate question and optional machine code
   |  2. read attachments into memory and extract text (uploads only)
   v
ChromaDB (backend/data/chroma_db, cosine, all-MiniLM-L6-v2 ONNX embeddings)
   |  top-5 incident IDs + distances, optional exact machine metadata filter
   v
distance gate: keep matches with distance <= 0.50
   v
PostgreSQL 17 (machines, downtime_incidents, downtime_resolutions)
   |  reload each match; keep only Closed records with machine + resolution
   v
Groq chat completion (default openai/gpt-oss-20b, temperature 0.2)
   v
citation validation + disclosure notes -> JSON response -> chat UI
```

Key properties of the pipeline:

- **ChromaDB is a candidate index, not the source of truth.** It returns incident IDs; the evidence
  sent to the model is rebuilt from the current PostgreSQL rows on every question.
- **No evidence means no LLM call** for the question-only endpoint. The API returns a fixed
  "couldn't find a sufficiently relevant resolved downtime incident" answer with empty sources.
- **Uploads never become evidence records.** Attachment text is added to the prompt as untrusted
  user data; it is not written to PostgreSQL, ChromaDB or disk, and it cannot be cited as history.
- **Importing is the only write path.** The import commit inserts eligible incidents and their
  resolutions in one transaction, then refreshes ChromaDB from PostgreSQL. A failed refresh leaves
  the committed rows in place and is reported as a separate indexing status.

## 3. Repository layout

```
machinecare-ai-starter/
├─ .env                      Local secrets (git-ignored, read from the repository root)
├─ compose.yaml              PostgreSQL 17 for local development
├─ backend/
│  ├─ app/
│  │  ├─ api/
│  │  │  ├─ main.py          RAG FastAPI app (used by the frontend)
│  │  │  ├─ auth.py          Login, refresh, logout, me and password routes
│  │  │  ├─ admin_users.py   Admin account management routes
│  │  │  ├─ incident_import.py          Template, machines, preview, commit and reindex routes
│  │  │  ├─ incident_import_schemas.py  Import request/response models
│  │  │  └─ schemas.py       Pydantic request/response models and limits
│  │  ├─ core/               Settings, Argon2id/JWT helpers, permissions, password policy
│  │  ├─ dependencies/auth.py  Bearer authentication and permission dependencies
│  │  ├─ main.py             Phase 1 database-only FastAPI app (separate)
│  │  ├─ db/database.py      Engine and session factory from DB_* variables
│  │  ├─ models/             Machine, DowntimeIncident, DowntimeResolution, User, Role, sessions
│  │  ├─ repositories/       Queries for closed incidents and lookups by ID
│  │  ├─ scripts/create_admin.py  Bootstrap administrator CLI
│  │  └─ services/
│  │     ├─ auth_service.py            Login, sessions, refresh rotation, password flows
│  │     ├─ user_admin_service.py      Admin account management
│  │     ├─ audit_service.py           Append-only security audit trail
│  │     ├─ rag_service.py             Retrieval, prompt building, citation checks
│  │     ├─ vector_store_service.py    ChromaDB indexing and search
│  │     ├─ groq_service.py            Groq chat completion call
│  │     ├─ document_extraction.py     In-memory attachment validation and text extraction
│  │     ├─ incident_import_service.py CSV/XLSX parsing, validation and row errors
│  │     ├─ incident_import_preview.py Read-only comparison of the rows with PostgreSQL
│  │     ├─ incident_import_commit.py  Single-transaction insert, then indexing
│  │     ├─ incident_import_template.py XLSX and CSV template generation
│  │     ├─ incident_import_digest.py  Upload SHA-256 that binds a preview to its commit
│  │     └─ incident_retrieval_service.py  JSON loading and keyword search helper
│  ├─ migrations/            Alembic environment and revisions
│  ├─ tests/                 pytest suite, fixtures, retrieval/rerank evaluations
│  ├─ data/
│  │  ├─ incidents.json      100 source records (tracked)
│  │  └─ chroma_db/          Persistent vector index (git-ignored)
│  ├─ alembic.ini            Alembic config (script_location = migrations)
│  ├─ pytest.ini             pythonpath = ., testpaths = tests
│  ├─ requirements.txt       Runtime dependencies
│  ├─ requirements-dev.txt   Test dependencies
│  ├─ seed_database.py       PostgreSQL seeding from incidents.json
│  ├─ index_incidents.py     ChromaDB indexing from PostgreSQL
│  └─ test_*.py              Manual smoke scripts (not collected by pytest)
└─ frontend/
   ├─ src/api/client.ts      Fetch layer with timeouts and error mapping
   ├─ src/api/importClient.ts Import endpoints, response checks and template downloads
   ├─ src/components/        Header, hero, chat thread, answer card, sources, composer, overlay,
   │                         import dialog
   ├─ src/hooks/             useAsk, useApiHealth, useAttachments, useFileDrop, useIncidentImport
   ├─ src/lib/files.ts       Client-side attachment validation
   ├─ src/lib/importFile.ts  Client-side import limits and template downloads
   ├─ src/types.ts           API types and shared limit constants
   ├─ src/styles.css         Dark charcoal/orange theme
   └─ vite.config.ts         Dev server on 5173, proxy to 127.0.0.1:8001
```

`backend/machinecare.db` may appear after starting the Phase 1 app; it is the default SQLite store
for that application and is git-ignored.

## 4. Technology stack

| Layer | Technology |
| --- | --- |
| Backend API | FastAPI, uvicorn (`uvicorn[standard]`), Pydantic 2 |
| Persistence | PostgreSQL 17, SQLAlchemy 2.1 ORM, psycopg 3 (`postgresql+psycopg`) |
| Migrations | Alembic (`6041a26eff31`, then `e5a7c1f90b34` users/roles/sessions, `b2d4f6a80c15` audit log) |
| Vector store | ChromaDB 1.5, persistent client, cosine space, default ONNX all-MiniLM-L6-v2 embeddings |
| Generation | Groq chat completions, default model `openai/gpt-oss-20b`, temperature `0.2` |
| Authentication | pwdlib Argon2id password hashing, PyJWT HS256 access tokens, rotating refresh sessions |
| Text extraction | pypdf (PDF), openpyxl (`.xlsx`), xlrd (`.xls`), standard library zipfile/XML (DOCX), standard library text/CSV |
| Frontend | React 19, TypeScript 5.8, Vite 7, react-markdown 10 with remark-gfm |
| Tests | pytest with FastAPI `TestClient`, in-memory SQLite fixtures |
| Local infrastructure | Docker Compose (PostgreSQL only; the apps run on the host) |

## 5. Prerequisites and environment configuration

Prerequisites:

- Windows with PowerShell (all commands below are PowerShell, so use `;` rather than `&&`).
- Python 3.11 or newer, verified with 3.12.10. Prefer a version that has wheels for every
  dependency (the examples below pin 3.12 through the Windows launcher).
- Node.js 20.19+ or 22.12+ (Vite 7 requirement; 24.x is what this environment uses) with npm.
- Docker Desktop for PostgreSQL, or another PostgreSQL 17 instance you configure yourself.
- A Groq API key and internet access. The key is required for answering; the first ChromaDB
  embedding call also downloads the ONNX MiniLM model (~80 MB) once.

Create `.env` in the repository root with these variables (`.env.example` in the repository root
shows the same keys with placeholders). `.env` is git-ignored; never commit it:

```dotenv
# PostgreSQL: compose.yaml requires POSTGRES_PASSWORD; the backend reads DB_*
POSTGRES_PASSWORD=replace-with-a-local-password
DB_USER=machinecare_user
DB_PASSWORD=replace-with-the-same-local-password
DB_HOST=127.0.0.1
DB_PORT=5434
DB_NAME=machinecare_db

# Groq: required for answer generation
GROQ_API_KEY=replace-with-your-groq-api-key
GROQ_MODEL=openai/gpt-oss-20b

# Authentication: required for login and every protected endpoint.
# Generate a value with:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"
JWT_SECRET=replace-with-a-long-random-value
# Mark the refresh cookie Secure; keep false for plain-HTTP localhost.
COOKIE_SECURE=false
```

Notes:

- `compose.yaml` fixes the database (`machinecare_db`) and user (`machinecare_user`) and exposes
  it on `127.0.0.1:5434`. `DB_PASSWORD` must match `POSTGRES_PASSWORD`, because the backend
  connects with `DB_USER`/`DB_PASSWORD` and Docker initializes that user with `POSTGRES_PASSWORD`.
- `DB_USER`, `DB_PASSWORD` and `DB_NAME` are required; `app/db/database.py` raises `KeyError` at
  import time when they are missing. `DB_HOST` defaults to `127.0.0.1` and `DB_PORT` to `5434`.
- `GROQ_MODEL` is optional; `openai/gpt-oss-20b` is the default in `groq_service.py`.
- `JWT_SECRET` is required for sign-in (section 7); a missing value surfaces as a clear error on
  the first login attempt rather than at startup. `COOKIE_SECURE` defaults to `true` and must stay
  `false` for plain-HTTP local development.
- `app/main.py` (the Phase 1 app) optionally reads `DATABASE_URL`. The RAG API never reads it.
- pytest does not need a running database, a vector index or a Groq key: `tests/conftest.py` pins
  dummy `DB_*` values, and tests use in-memory SQLite with mocked retrieval and generation.

## 6. Local setup and startup instructions

Run the setup from the repository root:

```powershell
cd C:\Projects\machinecare-ai-starter
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r backend\requirements.txt
python -m pip install -r backend\requirements-dev.txt
```

If Python 3.12 is not installed, substitute the launcher version you have available and confirm
that every dependency installs with wheels before continuing.

If PowerShell blocks activation, skip it and call the interpreter directly
(`.\.venv\Scripts\python.exe`) as shown in the commands below.

Start PostgreSQL:

```powershell
cd C:\Projects\machinecare-ai-starter
docker compose up -d
docker compose ps
```

Apply the schema, seed PostgreSQL from the JSON records, and build the vector index:

```powershell
cd C:\Projects\machinecare-ai-starter\backend
..\.venv\Scripts\python.exe -m alembic upgrade head
..\.venv\Scripts\python.exe seed_database.py
..\.venv\Scripts\python.exe index_incidents.py
```

Create the first administrator (sign-in has no self-service registration; the password is prompted
on the terminal, or read from `ADMIN_PASSWORD`, and is never an argv value):

```powershell
cd C:\Projects\machinecare-ai-starter\backend
..\.venv\Scripts\python.exe -m app.scripts.create_admin --username admin --display-name "Administrator"
```

Additional accounts are created by a signed-in admin through `POST /api/v1/admin/users` (section 7).

Start the RAG API on port **8001** (the port the frontend proxies to):

```powershell
cd C:\Projects\machinecare-ai-starter\backend
..\.venv\Scripts\python.exe -m uvicorn app.api.main:app --reload --port 8001
```

Start the frontend in a second terminal:

```powershell
cd C:\Projects\machinecare-ai-starter\frontend
npm install
npm run dev
```

Open http://localhost:5173 and sign in with an administrator-provisioned account. The header pill
reads "RAG API connected" when `GET /health` (proxied
to the RAG API) answers; it only proves the API process is running, not that PostgreSQL, ChromaDB
or Groq are ready.

Answering a question requires:

1. a running RAG API on port 8001,
2. PostgreSQL with the seeded records (`seed_database.py`),
3. a non-empty ChromaDB index built from those records (`index_incidents.py`),
4. a valid `GROQ_API_KEY` with network access,
5. a signed-in account: every role can ask questions; importing needs the maintenance-engineer or
   admin role (section 7).

## 7. Backend API applications and endpoints

Both applications are FastAPI apps, but they are started separately and read different configuration:
the RAG API uses the `DB_*` variables, while the Phase 1 app uses `DATABASE_URL` or SQLite.

| Application | Module | Port | Used by the frontend | Purpose |
| --- | --- | --- | --- | --- |
| RAG API | `app.api.main:app` | 8001 (`--port 8001`) | Yes | Question answering over PostgreSQL + ChromaDB + Groq, attachments, and the incident import |
| Phase 1 record API | `app.main:app` | 8000 (uvicorn default) | No | Read-only inspection of the original incident table |

### RAG API (`backend/app/api/main.py`)

| Method | Path | Request | Response |
| --- | --- | --- | --- |
| GET | `/health` | none | `{"status": "ok"}` (process liveness only) |
| POST | `/api/v1/ask` | JSON `{"question": str, "machine": str \| null}` | `AskResponse` |
| POST | `/api/v1/ask-with-files` | multipart `question`, optional `machine`, 1–3 `files` | `AskWithFilesResponse` |
| GET | `/docs`, `/openapi.json` | none | Interactive docs and schema |

Every `/api/v1` route requires a signed-in user: the ask endpoints need `assistant:use`, and the
import endpoints below need `incidents:import` or `index:run`. Requests without a valid token get
`401` with `WWW-Authenticate: Bearer`; signed-in users without the permission get `403`. The
frontend attaches the token and silently refreshes an expired one, but the API never relies on the
frontend for enforcement.

`AskResponse` fields:

- `answer` — generated Markdown, or the fixed no-evidence message.
- `sources` — `[{ "incident_id", "machine", "distance" }]` for the records used as evidence
  (`distance` rounded to four decimals).
- `cited_incident_ids` — IDs mentioned in the answer that exist in the evidence set.
- `unsupported_citations` — IDs mentioned in the answer that are not in the evidence set.
- `documents` — only on `/api/v1/ask-with-files`:
  `[{ "filename", "characters", "truncated" }]`.

Limits enforced by the API layer: question 1–2000 characters after trimming, machine at most 50
characters, blank machine treated as "no filter".

Example (PowerShell):

```powershell
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8001/api/v1/ask `
  -ContentType "application/json" `
  -Body '{"question":"AOI camera inspection is inaccurate","machine":"AOI-01"}'

curl.exe -s -X POST http://127.0.0.1:8001/api/v1/ask-with-files `
  -F "question=AOI camera is inaccurate" `
  -F "machine=AOI-01" `
  -F "files=@C:\path\to\report.pdf"
```

Error responses:

| Status | Cause |
| --- | --- |
| 400 | `/api/v1/ask-with-files` with no files, or more than 3 files |
| 413 | an upload exceeds 10 MB (detected while reading) or an archive expands too far |
| 415 | unsupported extension, declared content type mismatch, invalid file signature, binary TXT/CSV |
| 422 | blank question; empty or unreadable document; unsupported encoding; encrypted/mislabeled workbook; a workbook with no cell values |
| 500 | retrieval or generation failure; the client sees a generic message and the traceback is logged server-side |

### Incident import endpoints (`backend/app/api/incident_import.py`)

| Method | Path | Request | Response |
| --- | --- | --- | --- |
| GET | `/api/v1/incidents/import/template.xlsx` | query `format=xlsx` (default) or `format=csv` | the template file, sent with `Content-Disposition` |
| GET | `/api/v1/incidents/import/machines` | none | `{"machines": [...]}`, the machine codes a row may reference |
| POST | `/api/v1/incidents/import/preview` | multipart `file`, form `skip_existing` (default `false`) | `ImportPreviewResponse`, always 200 for a file that parses |
| POST | `/api/v1/incidents/import` | multipart `file`, form `file_sha256`, form `skip_existing` | `ImportCommitResponse` |
| POST | `/api/v1/incidents/import/reindex` | none | `ReindexResponse` |

`ImportPreviewResponse` fields: `file_name`, `file_sha256` (SHA-256 of the uploaded bytes),
`skip_existing`, `total_rows`, `valid_rows`, `invalid_rows`, `skipped_existing_rows`,
`eligible_rows`, `rows` (`row`, `incident_id`, `date`, `machine`, `production_line`,
`downtime_minutes`, `state`) and `errors` (`row`, `field`, `code`, `message`). Row-level problems
are the preview's answer, so they arrive with 200 rather than as an error status.

`ImportCommitResponse` fields: `file_name`, `file_sha256`, `skip_existing`, `imported_rows`,
`imported_incident_ids`, `skipped_existing_rows`, `database_committed`, and `indexing`
(`status` = `indexed` | `failed` | `skipped`, `collection_total`, `message`). `collection_total` is
the size of the whole ChromaDB collection after a refresh, not the number of rows the import added.

| Status | Cause |
| --- | --- |
| 409 | the upload does not match `file_sha256`, or the database changed after the preview; nothing is written |
| 413 | the upload exceeds the 5 MB import limit |
| 415 | the extension or file format is not CSV or XLSX, or the file signature does not match |
| 422 | bad or missing headers, an empty file, more than 5,000 data rows, or any row-level problem at commit time; nothing is written |
| 500 | the database write failed; nothing is written |
| 503 | a reindex attempt failed; rows committed by an earlier import stay in place |

These endpoints require authentication plus the `incidents:import` permission (reindex:
`index:run`) and are the only write path into PostgreSQL. Section 11 describes the UI workflow.

### Authentication and admin endpoints (Phase 1)

Sign-in is administrator-provisioned only. `POST /api/v1/auth/login` returns the access token in
the body and sets the refresh token as an HttpOnly `SameSite=strict` cookie scoped to
`/api/v1/auth`; the cookie is rotated on every refresh, and replaying a rotated-away cookie
revokes the whole session family.

| Method | Path | Access | Purpose |
| --- | --- | --- | --- |
| POST | `/api/v1/auth/login` | public | Verify credentials; access token in the body, refresh cookie set |
| POST | `/api/v1/auth/refresh` | refresh cookie | Rotate the session, return a fresh access token |
| POST | `/api/v1/auth/logout` | bearer token | Revoke the session named by the cookie and clear it |
| GET | `/api/v1/auth/me` | bearer token | The signed-in user with roles and effective permissions |
| POST | `/api/v1/auth/change-password` | bearer token | Set a new password; revokes all of the user's sessions |
| POST | `/api/v1/auth/reset-password` | one-time token | Consume an admin-issued reset token and set a new password |
| GET | `/api/v1/admin/users` | `users:manage` | List accounts |
| POST | `/api/v1/admin/users` | `users:manage` | Create an account (username, display name, role, initial password) |
| PATCH | `/api/v1/admin/users/{user_id}` | `users:manage` | Update display name, role or active flag |
| POST | `/api/v1/admin/users/{user_id}/reset-password` | `users:manage` | Issue a one-time reset token (shown once) |
| POST | `/api/v1/admin/users/{user_id}/disable` | `users:manage` | Disable the account and kill its sessions and tokens |
| GET | `/api/v1/admin/roles` | `users:manage` | The three fixed roles |

Roles and permissions are defined once in `backend/app/core/permissions.py` and enforced by the
backend on every request:

| Permission | Admin | Maintenance engineer | Viewer |
| --- | --- | --- | --- |
| `assistant:use` — ask endpoints | yes | yes | yes |
| `incidents:read` — read incident data | yes | yes | yes |
| `incidents:import` — import template, machines, preview, commit | yes | yes | no |
| `index:run` — reindex | yes | no | no |
| `users:manage` — account management | yes | no | no |
| `incidents:write` / `incidents:delete` / `audit:read` | yes (reserved) | no | no |

The last row's permissions are reserved for future endpoints; no route uses them yet.

### Phase 1 record API (`backend/app/main.py`)

This application predates the RAG pipeline and is independent of the Alembic schema. It declares
its own `incidents` table (flat columns), creates it with `Base.metadata.create_all` and seeds it
from `backend/data/incidents.json` on startup, skipping incident IDs that already exist. It uses
`DATABASE_URL` when set, otherwise a SQLite file at `backend/machinecare.db`.

| Method | Path | Notes |
| --- | --- | --- |
| GET | `/health` | `{"status": "ok", "phase": "database-foundation"}` |
| GET | `/incidents` | Query `machine` (case-insensitive partial match), `status` (exact), `limit` 1–100 (default 20); ordered by date then ID |
| GET | `/incidents/count` | Row count of this application's own table (100 after the first seed) |
| GET | `/incidents/{incident_id}` | Exact record or 404 |

It has no create, update or delete endpoints, no RAG, and it is **not** called by the frontend.

## 8. RAG retrieval, grounding, sources, and citations

Question-only flow (`ask_machinecare` in `backend/app/services/rag_service.py`):

1. **Dense retrieval.** `search_similar_incidents(question, top_k=5, machine)` queries the Chroma
   collection `machine_downtime_incidents` (cosine space) using ChromaDB's default ONNX
   all-MiniLM-L6-v2 embedding function. When a machine code is supplied it is passed as an exact
   metadata filter.
2. **Distance gate.** Matches with `distance > MAX_DISTANCE` (`0.50`) are discarded.
3. **Authoritative reload.** Each remaining ID is loaded from PostgreSQL with its machine and
   resolution. The record is dropped if it no longer exists, its status is not `Closed`, its
   machine or resolution is missing, or it does not match the requested machine filter. This keeps
   a stale ChromaDB entry from ever grounding an answer.
4. **No-evidence fallback.** If nothing survives, the service returns the fixed message
   "I couldn't find a sufficiently relevant resolved downtime incident in the database." with empty
   `sources` and citation lists, without calling Groq.
5. **Prompt construction.** The prompt contains the retrieved evidence as JSON plus rules requiring:
   historical findings separated from hypotheses, every historical finding citing an exact
   incident ID, possible causes labelled as hypotheses, corrective actions described only as
   recorded, preventive actions treated as past plans, no invented procedures/thresholds/part
   numbers, no claim that repairs succeeded, and incident records treated as untrusted data.
   The requested output sections are Similar Historical Incidents, Historical Root Causes,
   Previously Recorded Corrective Actions, Possible Causes (hypotheses), Recommended Next Steps
   (recommendations) and Sources.
6. **Generation.** `generate_response(prompt)` calls Groq chat completions with temperature `0.2`
   and the configured model.
7. **Citation check.** Incident IDs are extracted from the answer after normalizing typographic
   dashes, zero-width characters and spacing. Only IDs present in the evidence set are reported as
   `cited_incident_ids`; others become `unsupported_citations` and a note is appended to the answer.
8. **Document-reference guard.** If the answer mentions manuals, SOPs, work instructions,
   checklists, service bulletins or similar documents that are not in the evidence, a note is
   appended stating that only incident records are indexed and those references were not verified.
9. **Response.** `answer`, `sources`, `cited_incident_ids` and `unsupported_citations` are returned.

Document-aware flow (`ask_machinecare_with_documents`) uses the same retrieval, threshold and machine
filter, so an attachment cannot weaken grounding:

- Instruction-like lines in the uploaded text are replaced with `[instruction-like line removed]`
  before the prompt is built.
- Each document is wrapped in `<<<BEGIN UNTRUSTED DOCUMENT>>>` / `<<<END UNTRUSTED DOCUMENT>>>`
  markers, with at most **12,000 characters shared across all attachments** in one prompt.
- The model must separate document statements (attributed by filename, unverified) from incident
  history (with exact IDs) and recommendations; incident IDs that appear only in a document must
  not be cited.
- If no incident passes the threshold, generation still runs on the documents and a note is
  appended stating that the answer is based on the uploaded document only.

The UI mirrors this: the answer card marks verified citations and labels unsupported ones as
"unverified", and the sources rail lists the incidents used for the latest completed answer.

## 9. Chat UI and conversation behavior

- **First load.** The hero shows the illustration, heading and four suggested prompts. Prompts are
  static strings in `Hero.tsx`.
- **Starting a conversation.** Clicking a suggested prompt submits it immediately (the same path as
  pressing Ask) and focuses the composer. Typing or attaching files alone does not start a
  conversation. Once any turn exists, the hero collapses to a compact banner and the suggestions
  are hidden for the rest of the session, including for follow-up questions.
- **Composer.** Enter submits, Shift+Enter inserts a newline, and the Ask button submits the
  trimmed draft. The textarea is limited to 2000 characters and a counter appears past 1700.
- **Machine filter.** An optional free-text field (maximum 50 characters, placeholder `AOI-01`)
  is sent as `machine` with the question. It is matched exactly against `machines.machine_code`,
  so `aoi-01` or a partial name returns no evidence.
- **Header actions.** The header shows the API status pill and an `Import incidents` button that
  opens the import dialog (section 11). The button is separate from the chat: it never touches the
  draft question or the attached files.
- **Transcript.** Each exchange appends a user bubble (question, attachment names and sizes,
  machine filter) and an assistant answer. Answers render Markdown with GFM tables; raw HTML in
  the answer is escaped by react-markdown. A pending turn
  shows an animated "Retrieving evidence…" bubble, or "Reading documents and retrieving
  evidence…" when files were attached.
- **Errors and retry.** Failures render the message plus a "Try again" button; the retry reuses the
  original question, machine and files, so an upload does not have to be reselected.
- **Sources rail.** Shows the sources of the most recent completed answer with machine code,
  distance to three decimals, and a `cited` marker; before the first answer it shows an idle hint.
- **Health indicator.** `GET /health` is polled every 30 seconds and on window focus; a network
  failure during an ask marks the API offline immediately.
- **State.** The conversation is client-side only (`useAsk`), one request at a time, no streaming.
  A page refresh clears the thread, and follow-up questions are sent without earlier turns, so each
  answer is retrieved from the new question only.

## 10. File attachments and drag-and-drop

Client-side rules (`frontend/src/lib/files.ts`, constants in `frontend/src/types.ts`):

| Rule | Value |
| --- | --- |
| Formats | `.pdf`, `.txt`, `.csv`, `.docx`, `.xlsx`, `.xls` |
| Count | at most 3 files per question |
| Size | at most 10 MB (10 × 1024 × 1024 bytes) each |
| Empty files | rejected |
| Duplicate names | rejected (case-insensitive) |
| Question | 1–2000 characters |
| Machine | at most 50 characters |

UI behavior:

- Attached files appear as chips **above the question textarea**, each with a document icon, name,
  size, and a remove button; validation messages appear in the same area, above the textarea. The
  Attach button shows an `n/3` count.
- The Attach button and the hidden file input remain available. Attach is disabled while a request
  is pending and when three files are attached.
- Dropping files anywhere on the chat panel attaches them through the same `selection.add`
  validation path, shows a dashed orange overlay reading "Drop files to attach" with the supported
  formats and limits, and never submits the question. The overlay does not flicker when the pointer
  moves between nested children, is cleared by leaving the panel, dropping, cancelling the drag, or
  starting a request, and is ignored while an answer is in flight.
- File drags anywhere on the page are prevented from navigating the browser to the dropped file.
- With attachments the frontend calls `POST /api/v1/ask-with-files`; without attachments it calls
  `POST /api/v1/ask`.

Server-side validation and extraction (`backend/app/services/document_extraction.py`) run in memory
for every request; nothing is written to disk, PostgreSQL or ChromaDB.

1. **Extension** must be one of `.pdf`, `.txt`, `.csv`, `.docx`, `.xlsx`, `.xls` (otherwise 415).
2. **Declared content type** must be compatible with the extension unless the browser sent a generic
   type (otherwise 415).
3. **Size** must be non-zero and at most 10 MB (422 for empty, 413 for oversized).
4. **Signature** is checked: `%PDF-` for PDF, a ZIP containing `word/document.xml` for DOCX, a ZIP
   containing `xl/workbook.xml` for XLSX (encrypted files and macro-enabled workbooks are rejected),
   and the OLE2 compound-file header for XLS. Binary TXT/CSV content is rejected with 415.
5. **Extraction** is read-only and never executes anything: no macros, no formula calculation, no
   external links. Formula cells use the cached value; a formula without one becomes
   `[formula without cached value]`. Hidden worksheets are included and labelled `[hidden]`.

| Format | Reader | Caps |
| --- | --- | --- |
| PDF | pypdf | 300 pages |
| DOCX | zipfile + XML | body or expanded archive ≤ 20 MB |
| XLSX | openpyxl (read-only) | ≤ 50 MB uncompressed, ≤ 2,000 archive entries, 10 worksheets, 500 rows and 20 columns per worksheet, 4,000 cells, 400 characters per cell |
| XLS | xlrd | 10 worksheets, 500 rows and 20 columns per worksheet, 4,000 cells, 400 characters per cell |
| CSV | csv | 5,000 rows |
| TXT / CSV | stdlib | UTF-16, UTF-8 or Windows-1252 decoding |

Extracted text is capped at **20,000 characters per file** and **12,000 characters across all
attachments** in one prompt. Truncation is marked inline and reported as `truncated: true`.
Scanned/image-only PDFs are not OCRed and are rejected when no text can be extracted.

Attachments are **ad-hoc question context**: they are shown in the transcript, listed in the
answer's "Documents analyzed" block, and discarded when the request ends. They are not imported
into PostgreSQL, not indexed in ChromaDB, and not searchable in later questions.

## 11. Incident import from CSV or XLSX

The `Import incidents` button in the header opens a modal that adds closed downtime incidents in
bulk. It is separate from chat attachments in every way: it never submits a question, it has its own
API client (`frontend/src/api/importClient.ts`) and state machine
(`frontend/src/hooks/useIncidentImport.ts`), and closing the dialog discards the chosen file, the
check result and the outcome.

The import endpoints require the `incidents:import` permission (admin or maintenance engineer),
and the dialog only appears for users holding it; the API remains the authority. There is still no
TLS or rate limiting, so do not expose the stack on a shared or public network (section 14).

### File format and limits

Download the template from the dialog (`XLSX template` or `CSV template`) or from
`GET /api/v1/incidents/import/template.xlsx?format=csv`. Columns may be in any order and header names
are matched case-insensitively:

| Column | Required | Rules |
| --- | --- | --- |
| `Incident ID` | yes | unique; an existing ID is a row error unless `skip_existing` is on |
| `Date` | yes | calendar date such as `2026-07-01` |
| `Machine` | yes | an exact machine code that already exists, for example `AOI-01` |
| `Downtime (min)` | yes | whole number, 0–1440 |
| `Cause` | yes | text |
| `Action Taken` | yes | text |
| `Root Cause` | yes | text |
| `Status` | yes | `Closed` only; this version imports closed incidents |
| `Production Line`, `Product`, `Issue Category`, `Responsible Team`, `Preventive Action` | no | text |

| Rule | Value | Enforced by |
| --- | --- | --- |
| Formats | `.csv`, `.xlsx` | dialog and API |
| Size | 5 MB (5 × 1024 × 1024 bytes) | dialog as an early check, API as the authority (413) |
| Data rows | 5,000 | dialog copy and API (422) |
| Scanned rows | 20,000 | API only |
| XLSX archive | entry count and expanded size caps | API only |

The dialog rejects an unsupported extension, an empty file and an oversized file before sending
anything, because that is faster feedback. The API repeats every check and is authoritative.

### Workflow

1. **Choose the file** (`1 · Choose the file`). The chosen name and size appear next to the button; a
   rejected file shows an alert and no request is made.
2. **Options** (`2 · Options`). `Skip rows whose incident ID already exists` is off by default. With
   it off, an existing ID is a row error; with it on, that row is counted as skipped. Existing
   incidents are never updated or overwritten in either mode.
3. **Check the file** (`3 · Check the file`) posts to `/preview`. Checking is read-only: the API
   parses the rows, validates them and compares incident IDs and machine codes with the database.
   The summary shows `Data rows`, `Invalid`, `Skipped · already exist` and `Eligible to import` from
   the response, plus the short SHA-256 and the `skip_existing` value the response was produced
   with. Row-level problems appear in a table with row number, field and message (the first 50; the
   rest are summarised). Any invalid row blocks the import.
4. **Import** (`4 · Import`). `Import N rows` is enabled only when the preview has at least one
   eligible row and no invalid rows; otherwise the dialog explains what is blocking.

Changing the file, or toggling `skip_existing`, clears the preview, so the button cannot submit a
check that no longer describes what would be written. The dialog also refuses a commit whose preview
was produced with a different `skip_existing`.

### Confirmation

The commit posts the same `File` object plus the preview's `file_sha256` and `skip_existing` to
`POST /api/v1/incidents/import`. The API recomputes the digest, re-checks every row against the
database as it is now, and inserts the eligible incidents and their resolutions in **one
transaction**. If any row fails, nothing is written.

- **409** (`The import was not confirmed`) means the data changed since the check, or the upload no
  longer matches the digest. The dialog clears the preview and offers `Check the file again`.
- **Timeout or ambiguous network failure.** The request is never retried automatically, because the
  server may already have committed the transaction. The dialog says so explicitly and asks the user
  to check the incident records and the search index before importing the file again.
- **Duplicate submissions** are prevented by a single in-flight guard in the hook and by the
  disabled `Importing…` button. Closing the dialog, pressing Escape or clicking the backdrop is
  refused while a commit is running.
- **500** means the write failed and nothing was inserted.

### Result and indexing recovery

The result panel reports the two stages separately, because they can disagree:

- **Database stage**: `Database committed` with `Rows imported` and `Skipped · already exist`, plus
  the imported incident IDs (the first eight).
- **Indexing stage**: `Search indexing` with `Indexed`, `Needs attention` or `Skipped`. When indexing
  failed, the panel states that the rows were imported successfully but indexing needs attention, and
  offers `Retry indexing`, which calls `POST /api/v1/incidents/import/reindex`.

`Retry indexing` re-reads PostgreSQL and upserts every **Closed incident** into ChromaDB, not only the
rows just imported. It never inserts, updates or deletes PostgreSQL rows and it never clears the
collection. `Collection total` is labelled as the size of the whole vector collection, not the number
of rows that were indexed or imported.

After a result, `Close` dismisses the dialog and `Import another file` resets it to step 1.

## 12. Database setup, migrations, seeding, and vector indexing

PostgreSQL schema (created by the single Alembic revision `6041a26eff31`):

- `machines` — `id`, unique `machine_code`, `name`, optional `production_line`.
- `downtime_incidents` — `id`, unique `incident_id`, `machine_id` (FK), optional `production_line`,
  `date`, `product`, `issue_category`, `cause`, `downtime_minutes`, `responsible_team`, `status`.
- `downtime_resolutions` — 1:1 with an incident (`incident_id` FK, unique), with `root_cause`,
  `action_taken` and `preventive_action`.

Migrations:

```powershell
cd C:\Projects\machinecare-ai-starter\backend
..\.venv\Scripts\python.exe -m alembic upgrade head
..\.venv\Scripts\python.exe -m alembic current
```

Alembic must run from `backend/`: `alembic.ini` and `migrations/env.py` import the `app` package,
which reads the `DB_*` variables from the root `.env`. To change the schema, autogenerate a
revision, review it, then upgrade:

```powershell
cd C:\Projects\machinecare-ai-starter\backend
..\.venv\Scripts\python.exe -m alembic revision --autogenerate -m "describe the change"
..\.venv\Scripts\python.exe -m alembic upgrade head
```

Seeding and indexing are separate, manual steps. Nothing synchronizes the JSON file, PostgreSQL
and ChromaDB automatically, and the RAG API does not seed or index on startup.

```powershell
cd C:\Projects\machinecare-ai-starter\backend

# PostgreSQL: create missing machines and incidents from data/incidents.json
..\.venv\Scripts\python.exe seed_database.py

# ChromaDB: upsert every Closed incident (with its machine and resolution)
..\.venv\Scripts\python.exe index_incidents.py
```

- `seed_database.py` is idempotent by incident ID: it creates missing machines and incidents and
  **skips** existing ones, so it does not update records that were edited.
- `index_incidents.py` re-reads PostgreSQL, upserts documents into the persistent collection at
  `backend/data/chroma_db` (collection `machine_downtime_incidents`, cosine space), and prints the
  resulting collection count. Re-run it after changing incident text; it never removes IDs that no
  longer qualify, but those stale entries are filtered out at question time because every match is
  re-validated against PostgreSQL.
- The correct order is: migrate → seed → index. Indexing before seeding indexes nothing.
- The import needs the migrated schema and the seeded machine list: a row whose machine code does
  not exist is rejected, and imported rows become searchable only once the indexing step succeeds
  (section 11).

Operational notes:

- Data lives in the Docker volume `machinecare_pgdata`. `docker compose down` keeps it;
  `docker compose down -v` deletes the database and requires migrating and seeding again.
- `backend/data/chroma_db` is git-ignored. Deleting that directory and re-running
  `index_incidents.py` rebuilds the index from PostgreSQL.
- The Phase 1 app's SQLite/`DATABASE_URL` `incidents` table is a separate store; it does not feed
  the RAG API and is not migrated by Alembic.

## 13. Testing and troubleshooting

Backend tests need no running PostgreSQL, no populated index and no Groq key: fixtures use in-memory
SQLite and mock retrieval/generation. Importing the vector module may create the local
`backend/data/chroma_db` directory, but the tests never query it.

Run them with:

```powershell
cd C:\Projects\machinecare-ai-starter\backend
..\.venv\Scripts\python.exe -m pytest
```

The current suite reports **512 passed, 3 skipped** (one Starlette deprecation warning). The three
skipped tests are the import concurrency tests in `tests/test_import_concurrency_postgres.py`, which
need a disposable PostgreSQL database: they run only when `IMPORT_TEST_POSTGRES_URL` is set, and
refuse any database whose name does not contain `import_test`. Run pytest from `backend/`;
`pytest.ini` sets `testpaths = tests`, so running it from the repository root collects nothing.

Offline evaluation reports (isolated in-memory Chroma collections built from `incidents.json`; the
persistent collection is not queried, cleared or overwritten, and no Groq call is made):

```powershell
cd C:\Projects\machinecare-ai-starter\backend
..\.venv\Scripts\python.exe -m tests.retrieval_eval
..\.venv\Scripts\python.exe -m tests.rerank_eval
```

`tests/rerank_eval.py` is an experiment only: the hybrid/rerank configuration grid is calibrated on
the same 13 questions it scores, and no production module imports it. Production retrieval remains
dense top-5 plus the distance gate.

Frontend type-check and production build (there is no frontend unit-test runner):

```powershell
cd C:\Projects\machinecare-ai-starter\frontend
npm run build
```

The build runs `tsc --noEmit` first, so a type error fails it. There is no frontend test runner; the
import dialog is verified by hand against a mocked API, never against the development database.

Manual smoke scripts in `backend/` are optional and need live services:

```powershell
cd C:\Projects\machinecare-ai-starter\backend
..\.venv\Scripts\python.exe test_database.py        # PostgreSQL connectivity
..\.venv\Scripts\python.exe test_models.py          # ORM table registration
..\.venv\Scripts\python.exe test_repository.py      # Closed-incident queries
..\.venv\Scripts\python.exe test_embeddings.py      # Embedding model loads and returns vectors
..\.venv\Scripts\python.exe test_semantic_search.py # Similarity search only
..\.venv\Scripts\python.exe test_retrieval.py       # JSON keyword retrieval helper
..\.venv\Scripts\python.exe test_groq.py            # Groq connectivity
..\.venv\Scripts\python.exe test_rag.py             # Full RAG answer
```

Troubleshooting:

| Symptom | Likely cause and fix |
| --- | --- |
| `KeyError: 'DB_USER'` (or `DB_NAME`) at import | The root `.env` is missing or incomplete; create it and run from `backend/` so it is found |
| `OperationalError` / connection refused on port 5434 | PostgreSQL is not running; run `docker compose up -d` and check `docker compose ps` |
| Header pill offline, or "The RAG API could not be reached. Is it running on port 8001?" | The RAG API is not running on 8001 (uvicorn's default 8000 is not proxied) |
| "I couldn't find a sufficiently relevant resolved downtime incident…" | Empty or stale ChromaDB index, no record within distance `0.50`, or a machine filter that does not exactly match a code; re-run `seed_database.py` then `index_incidents.py`, and clear the machine field |
| 500 "Failed to answer the question." | Usually a missing/invalid `GROQ_API_KEY` or a Groq outage; the server log has the traceback |
| First indexing or first question is slow | ChromaDB is downloading the ONNX MiniLM embedding model; network access is needed once |
| pytest collects 0 tests | Run it from `backend/`, not the repository root |
| `ModuleNotFoundError: app` or Alembic cannot find models | Run `alembic`/scripts from `backend/` with the repository virtual environment |
| `.\.venv\Scripts\Activate.ps1` is blocked | Skip activation and call `..\.venv\Scripts\python.exe` directly |

## 14. Security and data handling

- **Secrets.** API keys and database credentials live only in the git-ignored root `.env`. They are
  never sent to the browser, and the frontend has no configuration file containing them. Do not
  commit `.env` or paste its values into logs, screenshots or issues.
- **Authentication.** Every `/api/v1` route requires a bearer access token (15-minute JWT, HS256)
  or, for refresh, the HttpOnly `SameSite=strict` refresh cookie scoped to `/api/v1/auth`.
  Passwords are hashed with Argon2id and never logged; refresh tokens are stored only as SHA-256
  digests and are rotated on every use — replaying an old one revokes the whole session family.
  Five failed logins lock the account with exponential backoff (60 s base, 900 s cap), and failed
  and successful logins, lockouts, logouts, password changes, reset-token issues and reuse
  detections are written to an append-only audit table that stores no passwords or tokens.
- **Authorization.** Permissions are code constants resolved from three fixed roles
  (`backend/app/core/permissions.py`): the admin holds all of them, the maintenance engineer may
  use the assistant and run imports, and the viewer may use the assistant and read answers. Checks
  run server-side through FastAPI dependencies; the frontend only hides what a user may not do.
- **Network exposure.** PostgreSQL is published on `127.0.0.1:5434` only. ChromaDB is a local
  directory, not a service. There is still no TLS or rate limiting on either FastAPI app, so treat
  the stack as local-development only and put it behind a TLS-terminating reverse proxy before
  sharing it.
- **Import endpoints.** `/api/v1/incidents/import/preview`, `/api/v1/incidents/import` and
  `/api/v1/incidents/import/reindex` require `incidents:import` / `index:run`, but the commit
  still writes to PostgreSQL: keep membership in the importing roles deliberate and the API on a
  local machine.
- **Uploads.** Files are read into memory in 64 KB chunks with an early size rejection, validated by
  extension, declared type and signature, parsed read-only, and discarded with the request. Nothing
  is written to disk, PostgreSQL or ChromaDB, and archive expansion is capped to resist zip bombs.
  No macro, formula, external link or embedded object is ever executed.
- **Prompt injection.** Attachment text is labelled as untrusted data, instruction-like lines are
  replaced before the prompt is built, and the prompt forbids following document instructions,
  changing role, or revealing secrets, environment variables, keys or the prompt itself.
- **Grounding.** The answer prompt may only use retrieved records, cited IDs are verified against
  the evidence set, unverified IDs are disclosed in the response and flagged in the UI, and the
  question-only flow discloses references to manuals/SOPs that are not indexed.
- **Logging and errors.** Request failures are logged server-side with a stack trace; clients
  receive generic messages. Extracted document text is not included in error responses.
- **Third-party processing.** Groq receives the question, the retrieved incident evidence and any
  extracted attachment text. Do not upload confidential company records or instructions to a
  third-party AI service without authorization. The sample dataset is development data.

## 15. Current limitations and planned features

Current limitations (verified against the code):

- No incident update or delete API. The import endpoint only inserts new closed incidents, never
  changes an existing one, and `seed_database.py` only inserts missing records, so editing a record
  still requires a manual SQL update.
- No per-row import provenance beyond the response of the request that inserted the rows: the import
  history is not stored, and the UI has no list of past imports.
- The import dialog has no automated frontend tests; it is checked by hand against a mocked API.
- No indexed SOP/manual knowledge base and no document management. Attachments are per-question
  context only, are not persisted, and are not searchable afterwards.
- No OCR: scanned or image-only PDFs are rejected when no text can be extracted.
- No TLS, rate limiting or MFA; the audit trail is write-only (the auth flows record events, and
  there is no reader UI or API for it yet).
- No streaming responses, no cancel button, one in-flight question at a time.
- No persisted conversations and no multi-turn context: a follow-up is retrieved and answered from
  its own text only, and a refresh clears the thread.
- Production retrieval is dense-only. The hybrid/rerank pipeline exists only as an offline
  experiment in `backend/tests/rerank_eval.py`.
- ChromaDB is not updated automatically when records change; `index_incidents.py` must be re-run.
- The machine filter requires an exact machine code; there is no machine list or autocomplete.
- No CI configuration, no container images for the two APIs, and no production serving/proxy setup
  for the built frontend (the Vite proxy is a development feature).
- Two independent data surfaces exist: the Alembic/PostgreSQL schema used by the RAG API and the
  Phase 1 app's own flat `incidents` table.

Planned features (not implemented yet):

1. Incident update/delete endpoints and per-row import provenance (who imported, when, which file
   digest).
2. Indexed SOPs and work instructions with versioning, provenance and permissions.
3. Promotion of hybrid retrieval/reranking into production after evaluation, with quality gates in
   CI.
4. Persisted conversations and streaming answers.
5. MFA for admin accounts, TLS/reverse-proxy deployment, and a reader for the audit log.
6. MCP read-only tools exposing approved queries.
7. Containerized deployment for both APIs and the built frontend.
