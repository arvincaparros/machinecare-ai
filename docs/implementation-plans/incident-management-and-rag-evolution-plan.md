# MachineCare AI — Incident Management and RAG Evolution Plan (Phases 4 and 7)

> Planning only. See `enterprise-architecture-assessment.md` for the legend.

---

## Part A — Incident management and data integrity (Phase 4)

### 1. Current state (verified)

| Fact | Location |
| --- | --- |
| Only inserts exist; no update/delete routes | `app/api/incident_import.py` |
| Commit is one transaction, then indexing | `incident_import_commit.py`, `_index_after_commit` |
| Reindex upserts all Closed incidents, never deletes | `vector_store_service.index_incidents` |
| Evidence is revalidated from PostgreSQL on every question | `rag_service._collect_evidence` |
| Deleting an incident would currently orphan/deny on the resolution FK | migration `6041a26eff31` has no `ondelete` |
| No timestamps or actor columns on incident tables | models |
| Concurrency tests exist for import only | `tests/test_import_concurrency_postgres.py` |

### 2. Incident CRUD design

#### 2.1 Endpoints (mounted on the RAG API, same `/api/v1` prefix)

| Method | Path | Permission | Notes |
| --- | --- | --- | --- |
| GET | `/api/v1/incidents` | `incidents:read` | filters: `machine`, `status`, `date_from`, `date_to`, `q`; cursor pagination |
| GET | `/api/v1/incidents/{incident_id}` | `incidents:read` | full record incl. resolution |
| PATCH | `/api/v1/incidents/{incident_id}` | `incidents:write` | partial update, `If-Match` on `version` |
| POST | `/api/v1/incidents/{incident_id}/archive` | `incidents:delete` | sets `archived_at` (soft delete) |
| DELETE | `/api/v1/incidents/{incident_id}` | `incidents:delete` | **[OPEN]** hard delete vs archive-only |
| POST | `/api/v1/incidents` | `incidents:write` | single-record create (import remains bulk) |

**[RECOMMENDATION]** Support **archive (soft delete)** as the default and expose hard delete
only behind an `[OPEN]` decision. Archiving sets `archived_at` and moves the status to a
non-`Closed` value. The RAG pipeline already excludes non-`Closed` records, so archiving
immediately removes an incident from evidence even before the vector is updated — a valuable
second line of defence.

Wait — **do not silently change `status`**. **[RECOMMENDATION]** Keep `status` semantically
the incident's lifecycle and add a separate `archived_at`. Retrieval must then filter
`status == 'Closed' AND archived_at IS NULL`. This requires a **deliberate, documented change
to `_collect_evidence`** (a one-line condition) and to `get_resolved_incidents`, both covered
by new tests. This is the only modification to the RAG read path in the whole roadmap.

#### 2.2 Schema changes (migration `c4_incident_management`)

```
downtime_incidents
  + version      INTEGER NOT NULL DEFAULT 1          -- optimistic concurrency
  + created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
  + updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
  + updated_by   BIGINT NULL FK users.id
  + archived_at  TIMESTAMPTZ NULL
  + INDEX (status, archived_at)
  + INDEX (machine_id)
downtime_resolutions
  + updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
  + FK incident_id ... ON DELETE CASCADE              -- added via new constraint
machines
  + created_at, updated_at
```

**Adding `ON DELETE CASCADE` to an existing FK** requires dropping and recreating the
constraint in the migration. This is reversible and must be reviewed; it changes what a
hard delete does (previously an error, now cascade). If archive-only is chosen, this change
is unnecessary.

#### 2.3 Validation

Reuse the import parser's field rules as the single source of truth:

- Reuse `MAX_DOWNTIME_MINUTES`, date parsing, required-field rules from
  `incident_import_service.py` by extracting a small `validate_incident_fields()` helper
  **without changing existing behaviour or signatures** (new function in a new module;
  the importer may later delegate, as a separate refactor).
- Machine must exist (`find_known_machine_codes`).
- `incident_id` immutable after creation **[RECOMMENDATION]**.

### 3. Vector synchronization

#### 3.1 Design: index job, not dual-write

```
index_jobs
  id            UUID PK
  kind          VARCHAR(16)  -- 'incident' | 'full'
  incident_id   VARCHAR(50) NULL     -- business id for kind='incident'
  operation     VARCHAR(16)  -- 'upsert' | 'delete'
  reason        VARCHAR(64)  -- create|update|archive|delete|import|manual
  status        VARCHAR(16)  -- 'queued' | 'running' | 'succeeded' | 'failed'
  attempts      INTEGER DEFAULT 0
  last_error    TEXT NULL            -- safe message, no traceback
  created_at, started_at, finished_at
  created_by    BIGINT NULL FK users.id
  INDEX (status, created_at)
```

Flow for every incident mutation:

1. In the **same database transaction** as the incident write, insert an `index_jobs` row
   (`queued`) plus an `audit_log` row.
2. Commit.
3. Attempt to process queued jobs **synchronously after commit** (same request), exactly as
   the import does today (`_index_after_commit`). This keeps the current UX where indexing
   usually completes before the response returns.
4. If processing fails, the job stays `queued`/`failed`. The response reports
   `indexing.status = failed` (matching `ImportIndexingInfo`).
5. A **worker** (in-process background task or a CLI `python -m app.scripts.process_index_jobs`)
   retries queued jobs. `POST /api/v1/admin/index/reindex` remains the manual full rebuild.

**[RECOMMENDATION]** Start with the in-request attempt + manual retry (identical semantics to
the import today) and add a periodic worker only when Phase 6 introduces scheduling. This
avoids a queue infrastructure dependency.

#### 3.2 Vector store operations to add

New functions in `vector_store_service.py` (additive, existing functions untouched):

| Function | Behaviour |
| --- | --- |
| `upsert_incident(incident)` | upsert one document by business ID |
| `delete_incidents(ids)` | `collection.delete(ids=...)` — needed because today nothing deletes |
| `reconcile()` | compare Chroma IDs with `get_resolved_incidents`; delete stale, upsert missing; returns a report |

**[RECOMMENDATION]** `reconcile()` is the authoritative recovery tool: PostgreSQL is the
source of truth, Chroma is derived. It is idempotent and safe to run any time.

#### 3.3 Failure-mode matrix (explicit)

| Scenario | Guaranteed state | User-visible result | Recovery |
| --- | --- | --- | --- |
| PostgreSQL update commits, Chroma upsert fails | DB has new values; vector stale | `indexing.status=failed`; answer still correct because evidence is reloaded from DB | index job retry / reconcile |
| Archive commits, Chroma delete fails | DB excludes incident; stale vector entry remains | incident never appears as evidence (`_collect_evidence` filter) | index job retry / reconcile |
| PostgreSQL write fails | nothing written, no job, no audit | error, nothing changed | user retries |
| Index job lost (process crash after commit, before attempt) | job row `queued` | response may have said failed | worker or admin reindex |
| Reindex times out | some IDs upserted, none deleted | `503` as today | job marked failed; retry/reconcile |
| Concurrent updates to one incident | last writer wins **only if** `version` matches; otherwise `409 Conflict` | client reloads and retries | optimistic concurrency |
| Two imports commit simultaneously | unique constraint resolves (existing) | 409 as today | unchanged |
| Update and archive race | version check rejects the loser | 409 | unchanged |

**[RECOMMENDATION]** Do **not** implement "delete from Postgres then delete from Chroma" as
an unordered dual write. The job row + reconcile design makes every step recoverable from
PostgreSQL alone.

### 4. Audit integration

Every incident mutation writes (same transaction):

```
action: incident.created | incident.updated | incident.archived | incident.deleted
resource_type: 'downtime_incident'
resource_id: incident_id
metadata: { "changed_fields": [...], "before": {...subset...}, "after": {...subset...} }
```

**[RECOMMENDATION]** Store changed field names and a **bounded** before/after diff (only
fields that changed, truncated). Do not store the whole record on every save.

### 5. Testing (Phase 4)

| Module | Cases |
| --- | --- |
| `test_incidents_api.py` | list filters/pagination; get; 404 for unknown |
| `test_incidents_update.py` | valid patch; validation errors; version conflict 409; audit row; index job created |
| `test_incidents_archive_delete.py` | archive excludes from retrieval; `_collect_evidence` ignores archived; delete cascade behaviour |
| `test_index_jobs.py` | job created in transaction; success/failure transitions; retry idempotent |
| `test_vector_reconcile.py` | stale vector IDs deleted; missing upserted; report counts |
| `test_incident_concurrency.py` | Postgres-only: concurrent PATCH, update+archive race (extend existing concurrency harness) |
| `test_rag_after_update.py` | updated root cause appears in next answer without manual reindex (extends `test_rag_freshness`) |

---

## Part B — RAG evolution (Phase 7)

### 6. Baseline (verified)

| Aspect | Current |
| --- | --- |
| Retrieval | dense cosine, top-5, `MAX_DISTANCE = 0.50` |
| Evaluation set | `tests/retrieval_eval_questions.json`: **13 questions**, 3 categories |
| Metrics implemented | Recall@5, Precision@5, MRR, no-evidence pass rate, filtered variants, threshold sweep |
| Hybrid experiment | `tests/rerank_eval.py` — IDF lexical + fusion + rerank; **not imported by app/** |
| Calibration risk | the experiment's own coefficients are **tuned on the same 13 questions it scores** (stated in `rerank_eval.py` docstring) |
| Machine filter | exact match, free text, no autocomplete |
| OCR | none |

**Conclusion:** the production retriever should **not** change on the strength of the
current experiment. The first Phase 7 deliverable is a trustworthy evaluation, not a new
retriever.

### 7. Evaluation programme

#### 7.1 Dataset split

| Split | Size target | Use |
| --- | --- | --- |
| `dev` | existing 13 + ~40 more | tuning, fast loops |
| `heldout` | 40–60 new cases, **never used for tuning**, owned by a different author | promotion gate |
| `no-evidence` | ≥ 15 cases | verify the 0.50 gate still suppresses unsupported retrieval |

Each case keeps the current schema (`id`, `category`, `question`, `machine`,
`expected_incident_ids`, `evidence`) so the existing harness can load it. Store splits as
separate JSON files (`retrieval_eval_questions.dev.json`, `.heldout.json`) and keep the
current file name pointing at dev for backward compatibility.

**[RECOMMENDATION]** Ground truth must be constructed by a human reading each incident, not
by running the retriever. Also record **acceptable-but-not-expected** IDs (the incident may
legitimately answer the question) so precision is not punished for correct retrieval.

#### 7.2 Metrics and gates

| Metric | Meaning | Gate for promotion |
| --- | --- | --- |
| Recall@5 | expected IDs retrieved | ≥ baseline on heldout |
| MRR | first relevant rank | ≥ baseline on heldout |
| Filtered recall@5 | after distance gate | no regression beyond noise |
| No-evidence pass rate | irrelevant matches suppressed | 100% on the no-evidence split |
| Citation correctness | answer IDs ⊆ evidence IDs (existing validator) | 100% (must remain) |
| Groundedness | each historical claim has a valid ID | sampled human review, no invented procedures |
| Answer relevance | LLM-judge or human rubric | informational, not a hard gate in v1 |
| Latency | added retrieval ms | ≤ +50 ms p95 for fusion |

**[RECOMMENDATION]** Regressions are judged on the **heldout** split only; dev numbers are
reported but never gate.

#### 7.3 Regression evaluation in CI

Add `python -m tests.retrieval_eval --questions <split> --json out.json` producing a JSON
report; CI stores it as an artifact and fails on thresholds. No LLM calls are needed for
retrieval metrics (the harness already avoids Groq). Answer-quality evaluation requires a
key and runs nightly, not per-PR (**[OPEN]** budget).

### 8. Hybrid retrieval

#### 8.1 Design

```
query ──┬─ dense: Chroma top K_D (e.g. 20) ──┐
        └─ lexical: BM25 over incident docs ┐
                                            ├─ RRF fusion (k=60) ──> top 5
                                            ┘
                                            └─ optional cross-encoder rerank (deferred)
```

- BM25 corpus is built from the same `build_incident_text` documents already indexed.
  **[RECOMMENDATION]** Keep BM25 in-process (a small inverted index rebuilt on index jobs) to
  avoid new infrastructure; `rank-bm25` is sufficient at this corpus size.
- Fusion: **Reciprocal Rank Fusion** is parameter-light and robust; prefer it over the
  hand-tuned alpha fusion in `rerank_eval.py`, which risks overfitting.
- The distance gate is **retained** for dense candidates; lexical-only hits must still be
  revalidated from PostgreSQL, identical to today.
- Machine filter is applied to **both** retrievers.

#### 8.2 Promotion rule

Hybrid retrieval ships behind a settings flag (`RETRIEVAL_MODE = dense | hybrid`) and is
promoted only when §7.2 gates pass on the heldout split. Until then production stays dense.

#### 8.3 Reranking

**[RECOMMENDATION]** Defer. A cross-encoder adds latency and a model dependency; only
consider after hybrid is measured. Use `tests/rerank_eval.py` as the starting harness but
**retrain/tune on dev only**.

### 9. Machine selection UX

- New endpoint `GET /api/v1/machines?q=` (reuse `get_machines`) → typeahead in the composer.
- Accept exact code only for retrieval (unchanged) but provide suggestions so users stop
  typing `aoi-01`.
- **[RECOMMENDATION]** Show "No incidents for this machine" when an exact code has no Closed
  incidents, instead of the generic no-evidence message.

### 10. OCR and document processing

| Item | Recommendation |
| --- | --- |
| OCR engine | Tesseract via `pytesseract` + `pdf2image`, or `ocrmypdf` | 
| Scope | scanned/image-only PDFs rejected today; OCR only when a PDF has no text layer |
| Limits | same 10 MB / page caps; OCR runs off-thread with a timeout |
| Privacy | remains in-memory; no OCR output persisted in v1 |
| Provenance | `documents[]` gains `ocr_used: true` so answers disclose OCR text is machine-read |
| Dependency | system package required in Docker image (Phase 6) — **[OPEN]** approval |

**[RECOMMENDATION]** Do not index uploaded documents (SOP/manual knowledge base) in this
roadmap phase. The brief's target list does not include a document corpus, and adding one
would change grounding semantics. Keep as a future initiative.

### 11. Reliability invariants to re-test after any RAG change

- No evidence ⇒ no LLM call.
- Evidence JSON contains only PostgreSQL records.
- Unsupported citations flagged and disclosed.
- Document guard behaviour unchanged.
- Distance gate honoured for dense hits.
- Machine filter enforced post-retrieval.

These map directly onto existing `test_rag_grounding.py`, `test_rag_freshness.py`
and `test_rag_documents.py`; new retrievers must pass them unchanged.

### 12. Acceptance criteria

**Incident management**
- [ ] Admin can update a record; the next question uses the new values without a manual
      reindex (DB is authoritative) and the vector is updated or queued.
- [ ] Archived incidents never appear as evidence, even if the vector is stale.
- [ ] Every mutation produces an audit row and an index job in the same transaction.
- [ ] Optimistic concurrency returns 409 on stale `version`.
- [ ] `reconcile()` repairs a deliberately corrupted vector store.

**RAG quality**
- [ ] A heldout evaluation set exists with human-verified ground truth.
- [ ] CI runs the evaluation and fails on regressions beyond agreed thresholds.
- [ ] Hybrid retrieval is measured on dev and heldout separately, with the numbers recorded.
- [ ] Production retrieval behaviour changes only after explicit approval of the heldout report.
- [ ] All reliability invariants (§11) still pass.

### 13. Open questions

1. Hard delete allowed for Admins, or archive-only? (recommended: archive-only.)
2. Is `incident_id` editable? (recommended: no.)
3. Who authors the heldout set, and how is it kept out of tuning? (process decision.)
4. Is an LLM-judge acceptable for answer quality, or human-only? (budget decision.)
5. OCR dependency approval and whether OCR text may be persisted in the future.
6. BM25 in-process vs a separate search service (recommended: in-process).