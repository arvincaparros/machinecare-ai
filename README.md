# MachineCare AI — Phase 1 Starter

**Goal:** Import 100 historical downtime incidents from the supplied workbook into a queryable database. This is a **database API**, not RAG yet.

## Repository layout
```
backend/      Python backend: FastAPI apps, RAG services, models,
              migrations, tests, seed data and the ChromaDB index
frontend/     React + TypeScript + Vite chat UI for the RAG API
compose.yaml  PostgreSQL 17 for local development
.env          Local secrets, git-ignored, read from the repository root
.venv/        Local virtual environment, git-ignored
```

## Requirements
- Python 3.11+ and VS Code
- Node.js 20+ and npm (frontend only)
- Docker Desktop (optional, only for PostgreSQL)

## Windows PowerShell setup
```powershell
cd machinecare-ai-starter
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
python -m pip install -r backend\requirements-dev.txt
cd backend
python -m uvicorn app.main:app --reload
```

If PowerShell blocks activation, install from the repository root with
`.\.venv\Scripts\python.exe -m pip install -r backend\requirements.txt`, then
`cd backend` and use `..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload`.

## Backend commands (run from `backend/`)
| Task | Command |
| --- | --- |
| Phase 1 record API | `python -m uvicorn app.main:app --reload` |
| RAG API (`/health`, `/api/v1/ask`, `/api/v1/ask-with-files`) | `python -m uvicorn app.api.main:app --reload` |
| Full test suite | `python -m pytest` |
| Retrieval evaluation report | `python -m tests.retrieval_eval` |
| Seed PostgreSQL from JSON | `python seed_database.py` |
| Index incidents into ChromaDB | `python index_incidents.py` |
| Apply migrations | `python -m alembic upgrade head` |
| Manual smoke scripts | `python test_rag.py`, `python test_repository.py`, … |

Start PostgreSQL from the repository root with `docker compose up -d`; the
compose file reads its password from the root `.env`. Alembic, pytest and the
scripts resolve `app` as a package relative to `backend/`, so always run them
from inside that directory.

## Frontend (run from `frontend/`)
The UI calls the same-origin paths `/api/v1/ask`, `/api/v1/ask-with-files` and
`/health`; Vite proxies them to the RAG API on port **8001**, so start that app
first.

```powershell
# terminal 1 — from backend/
..\.venv\Scripts\python.exe -m uvicorn app.api.main:app --port 8001

# terminal 2 — from frontend/
npm install
npm run dev      # http://localhost:5173
npm run build    # type-check + production bundle in dist/
```

Answering a question needs PostgreSQL, the ChromaDB index in `backend/data/`
and a Groq key in `.env`; `/health` reports process liveness only.

## Document attachments
`POST /api/v1/ask-with-files` accepts multipart form data instead of JSON:

| Field | Rules |
| --- | --- |
| `question` | required, 1–2000 characters after trimming |
| `machine` | optional filter, up to 50 characters |
| `files` | 1–3 files, ≤ 10 MB each, `.pdf`, `.txt`, `.csv`, `.docx`, `.xlsx` or `.xls` |

In the UI, files are attached with the Attach button or by dragging them onto
the chat panel; both paths run the same client-side validation and limits,
and dropping a file never submits the question.

The response is the `/api/v1/ask` payload plus a `documents` array
(`filename`, `characters`, `truncated`). Uploads are validated by extension,
declared content type and file signature, then extracted in memory with
`pypdf` (PDF), `openpyxl` (`.xlsx`), `xlrd` (legacy `.xls`) and the standard
library (TXT, CSV, DOCX). Nothing is written to disk, PostgreSQL or ChromaDB,
and extracted text is capped at 20,000 characters per file (12,000 characters
across all attachments in one prompt). Document text is treated as untrusted
data: instruction-like lines are replaced before the prompt is built, and the
model is told never to present a document as verified.

Workbooks are opened read-only and never executed: no macros, no formula
calculation and no external links. Each worksheet becomes a `### Worksheet:`
block with `Row N:` lines, in workbook order. Hidden and
very-hidden worksheets are included and labelled `[hidden]`; hidden rows and
columns cannot be distinguished in read-only mode, so their values are
included like any other cell. Formula cells use the cached result stored in the
file, and a formula without a cached result is written as
`[formula without cached value]`. Limits per workbook: 10 worksheets, 500 rows
and 20 columns per worksheet, 4,000 populated cells, 400 characters per cell.
Truncation is marked inline (`[worksheet truncated …]`, `[… extracted text
limit reached …]`) and reported as `truncated: true` in the response; a
workbook with no cell values is rejected with `422`.

## Verify
- http://127.0.0.1:8001/docs — interactive API documentation
- http://127.0.0.1:8001/health — RAG API status
- http://127.0.0.1:8001/api/v1/ask — RAG question endpoint (POST, JSON)
- http://127.0.0.1:8001/api/v1/ask-with-files — question plus documents (POST, multipart)
- http://127.0.0.1:8000/incidents/count — expected count 100
- http://127.0.0.1:8000/incidents/DT-2026-001 — exact incident
- http://127.0.0.1:8000/incidents?machine=AOI — filter by machine
- http://localhost:5173 — frontend dev server (needs the RAG API on :8001)

## Architecture decisions
- **SQLAlchemy ORM**: start with local SQLite (`backend/machinecare.db`) to eliminate initial setup; migrate to PostgreSQL using `DATABASE_URL` and an appropriate PostgreSQL driver later.
- **Source of truth**: the 100 incidents are preserved in `backend/data/incidents.json` and loaded idempotently on startup.
- **No AI yet**: RAG index, document ingestion, permissions and LLM come in later steps. Exact incident lookup must work first.
- **Data handling**: this sample is treated as a development dataset. Do not upload confidential company records or instructions to third-party AI services without authorization.

## Next build steps
1. Incident CRUD and ingestion validation
2. Vector indexing of *closed/verified* incidents and hybrid retrieval (structured filter + semantic similarity)
3. File-upload pipeline for SOP PDF / DOCX / XLSX / XLS with provenance and permissions (ad-hoc question attachments are supported; indexed SOPs are not)
4. Retrieval evaluation with repeat-fault questions; cite record and document page
5. LLM answer generation with grounding and insufficient-evidence behavior
6. MCP server exposing approved read tools

## Python concepts learned
- `class` and type hints define the domain model
- `with Session(engine)` manages database sessions
- functions decorated by `@app.get` become HTTP endpoints
- `select(...)` builds parameterized SQL queries
- `asynccontextmanager` manages startup tasks
