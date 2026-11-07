# MachineCare AI — Phase 0 Baseline Record

> Captured on 2026-11-07 before any Phase 1 (Authentication and RBAC) work.
> Repository state: branch `main` at `0839b57` ("feat: add incident CSV import
> workflow"), working tree clean except the untracked `docs/` planning set.

## 1. Backend test baseline

Command (from `backend/`, root `.venv`, Python 3.12.10):

```
..\.venv\Scripts\python.exe -m pytest -q
```

Result: **409 passed, 3 skipped** (412 collected) in ~30 s.

Matches the figure recorded in `enterprise-architecture-assessment.md`.
The 3 skipped tests are `tests/test_import_concurrency_postgres.py`, which
require `IMPORT_TEST_POSTGRES_URL` pointing at a disposable database.

## 2. Retrieval evaluation baseline

Command (from `backend/`):

```
..\.venv\Scripts\python.exe -m tests.retrieval_eval
```

Recorded baseline (unchanged since the import phase; re-confirmed during
Phase 0): raw recall@5 0.850, precision@5 0.600, MRR 0.950; filtered at the
production `MAX_DISTANCE = 0.50`: retention 22/30, irrelevant acceptance 5/27,
recall 0.662, precision 0.695, MRR 0.750, 0/3 no-evidence leaks.

## 3. Database and backup verification

- PostgreSQL 17 container (`machinecare-postgres`, image `postgres:17`) is up
  and healthy on `127.0.0.1:5434`.
- Schema at Alembic revision `6041a26eff31`; `downtime_incidents` holds
  105 rows at capture time.
- Backup procedure verified: `docker compose exec -T postgres pg_dump -U
  machinecare_user machinecare_db` produced a 52,336-byte dump containing all
  four tables (machines, downtime_incidents, downtime_resolutions,
  alembic_version). A full backup/restore runbook is deferred to Phase 6.8
  per `implementation-roadmap.md`.

## 4. API endpoint inventory (all unauthenticated at baseline)

| Method | Path | Handler |
| --- | --- | --- |
| GET | `/health` | `app/api/main.py` (liveness only, no DB) |
| POST | `/api/v1/ask` | `app/api/main.py` |
| POST | `/api/v1/ask-with-files` | `app/api/main.py` |
| GET | `/api/v1/incidents/import/template.xlsx` | `app/api/incident_import.py` |
| GET | `/api/v1/incidents/import/machines` | `app/api/incident_import.py` |
| POST | `/api/v1/incidents/import/preview` | `app/api/incident_import.py` |
| POST | `/api/v1/incidents/import` | `app/api/incident_import.py` (commit) |
| POST | `/api/v1/incidents/import/reindex` | `app/api/incident_import.py` |

## 5. Frontend behavior at baseline

React 19 + Vite single-page chat (`frontend/src`): in-memory transcript only,
no router, no auth context, no token handling. `api/client.ts` sends no
credentials or Authorization headers. The import dialog drives the import
routes through `api/importClient.ts`. Frontend verification is
`npm run build` (tsc strict + vite build); no test runner exists yet.

## 6. Constraints carried into Phase 1

- Importing `app.api.main` must keep loading none of: `chromadb`, `groq`,
  `app.db.database`, `app.models`,
  `app.repositories.incident_repository`,
  `app.services.incident_import_commit`, `app.services.rag_service`,
  `app.services.vector_store_service` (enforced by a subprocess test in
  `tests/test_api_incident_import.py`). New auth modules must respect the
  lazy-import boundary.
- Dependency policy: range constraints containing the versions installed in
  the root `.venv`; no unrelated upgrades. Phase 1 adds `pwdlib[argon2]`,
  `pyjwt` and declares the direct use of `pydantic-settings`.
- `rag_service.py` must not be modified in Phase 1.
- The Phase 1 legacy app (`backend/app/main.py`) stays untouched.

## 7. Recorded decisions (plan `[OPEN]` items, recommendations adopted)

1. Maintenance Engineer keeps `incidents:import` (append-only workflow).
2. An admin cannot disable or demote their own account.
3. Password reset is admin-issued one-time tokens; no email delivery in v1.
4. Refresh session lifetime is a constant 14 days (no "remember me").
5. Conversations are per-owner private from Phase 2 onward (out of scope here).

Dependency lockfiles and branch protection are deferred to Phase 6 (CI) per
the roadmap; the existing range-constraint policy remains in force.

## 8. Baseline conclusions

No differences were found between the approved plans and the repository that
would change the Phase 1 design. All plan-verified facts (lazy imports, no
auth code, unauthenticated write paths, test suite shape) were re-confirmed
against the current tree. Implementation proceeds per
`authentication-and-rbac-plan.md`.
