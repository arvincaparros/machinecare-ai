# MachineCare AI — Implementation Roadmap

> Planning only. No branches were created, no code was changed. See
> `enterprise-architecture-assessment.md` for the legend and verified baseline.

---

## 1. Recommended order (adjusted from the brief)

The brief's proposed order is sound. Two adjustments are made from verified dependencies:

1. **Phase 0 becomes a real gate**, not a formality: it includes capturing a baseline test
   run, pinning dependencies, and recording the retrieval baseline before anything moves.
2. **Auth precedes conversations** (already in the brief) because conversation ownership
   requires a user identity. **Streaming moves after conversations** (§brief has it after
   conversation persistence too) because it depends on the message status model.
3. **Incident management moves after multi-turn RAG** (per brief), but its vector
   reconciliation tool (`reconcile()`) is built early in Phase 4 and reused by Phase 7.
4. **Advanced RAG is last** and is explicitly blocked behind a held-out evaluation set.

```
Phase 0  Baseline & decisions            (docs + branch protection; no code)
   |
Phase 1  Auth + RBAC + audit skeleton    ── required by every later phase
   |
Phase 2  Conversations + persistence     ── requires users
   |
Phase 3  Multi-turn RAG                  ── requires conversations
   |
Phase 4  Incident CRUD + vector sync     ── requires permissions; uses reconcile()
   |
Phase 5  Streaming                       ── requires message status + conversations
   |
Phase 6  Production readiness            ── CI can start during Phase 2; Docker after 5
   |
Phase 7  Advanced RAG                    ── requires held-out eval set (built in Phase 6 CI)
```

**Dependency notes**

| Item | Blocked by |
| --- | --- |
| Conversation ownership | Phase 1 users |
| Audit rows with actor | Phase 1 audit skeleton |
| Streaming persistence | Phase 2 status enum |
| Frontend tests in CI | Phase 6 Vitest setup (can begin in Phase 2 for pure hooks) |
| Hybrid retrieval promotion | Phase 7 held-out set + Phase 6 eval gate |

---

## 2. Phase 0 — Baseline and decisions (1–2 days)

| Task | Output | Acceptance |
| --- | --- | --- |
| Capture baseline | `pytest` result (412 collected / 409 pass / 3 skipped) recorded | reproducible command in README |
| Capture retrieval baseline | run `tests.retrieval_eval` and archive its JSON report | numbers stored as the promotion baseline |
| Approve decisions | answers to all `[OPEN]` items in the five plans | recorded in a decision log |
| Pin dependencies | proposal for lockfiles (`pip-compile`, `npm ci`) | approved |
| Branch protection | require CI on `main` once Phase 6 exists | configured |

**Complexity:** XS. **No code changes.**

---

## 3. Phase 1 — Authentication and RBAC

| Task | Branch | Files | Migration | Tests |
| --- | --- | --- | --- | --- |
| 1.1 Settings + security primitives | `feat/auth-security-core` | `app/core/settings.py` (new), `app/core/security.py` (new) | none | password + token unit tests |
| 1.2 User/role/session models + migration | `feat/auth-db-models` | `app/models/user.py`, `role.py`, `refresh_session.py` (new), `app/models/__init__.py` (modify), migration `a1_users_roles` | create 5 tables, seed roles | model tests |
| 1.3 Login/refresh/logout/me routes | `feat/auth-endpoints` | `app/api/auth.py`, `app/schemas/auth.py`, `app/services/auth_service.py` (new), `main.py` (modify) | none | `test_auth_login`, `test_auth_tokens`, `test_auth_refresh` |
| 1.4 RBAC dependencies + protect routes | `feat/rbac-enforcement` | `app/dependencies/auth.py` (new), `main.py`, `incident_import.py` (modify) | none | `test_auth_rbac`, `test_auth_endpoints_guard`; update existing tests with auth fixture |
| 1.5 Admin user management | `feat/auth-admin-users` | `app/api/admin_users.py` (new), `app/services/user_admin_service.py` (new) | none | `test_admin_users` |
| 1.6 Audit skeleton | `feat/audit-foundation` | `app/models/audit_log.py`, `app/services/audit_service.py`, `app/middleware/request_id.py` (new) | create `audit_log` (+ `password_reset_tokens` if not in 1.2) | `test_audit_auth` |
| 1.7 Frontend login | `feat/frontend-auth` | `src/auth/*`, `src/pages/LoginPage.tsx`, `src/api/authClient.ts` (new), `src/api/client.ts`, `App.tsx`, `main.tsx`, `Header.tsx` (modify), `package.json` | none | Vitest login/protected-route |
| 1.8 Bootstrap admin CLI | `feat/auth-bootstrap-admin` | `app/scripts/create_admin.py` (new) | none | CLI test |

**Migration sequence:** `a1_users_roles` → (1.6) `a2_audit_log`.
**Complexity:** L. **Risks:** breaking existing tests (mitigated by shared auth fixture);
`JWT_SECRET` missing at startup (mitigated by lazy settings).

**Definition of Done:** matrix in `authentication-and-rbac-plan.md` §4.3 enforced; all
endpoints in §5.1 behave as listed; no plaintext secrets logged; existing tests pass
unchanged in assertions.

---

## 4. Phase 2 — Persistent conversations

| Task | Branch | Files | Migration | Tests |
| --- | --- | --- | --- | --- |
| 2.1 Conversation/message models | `feat/conversation-db-models` | `app/models/conversation.py`, `message.py` (new), `models/__init__.py` | `b2_conversations_messages` | model tests |
| 2.2 Repository + ownership queries | `feat/conversation-repository` | `app/repositories/conversation_repository.py` (new) | none | ownership query tests |
| 2.3 Conversation service + routes | `feat/conversation-api` | `app/api/conversations.py`, `app/schemas/conversation.py`, `app/services/conversation_service.py` (new), `main.py` (modify) | none | `test_conversations_api`, `test_conversation_ownership`, `test_messages_persistence`, `test_messages_failure` |
| 2.4 Message send wrapping RAG (non-streaming) | `feat/conversation-ask` | `app/services/conversation_service.py` | none | `test_conversation_citations`, `test_attachments_metadata_only` |
| 2.5 Frontend client + types | `feat/frontend-conversation-client` | `src/api/conversationClient.ts`, `src/types.ts` (modify) | none | unit tests |
| 2.6 Sidebar + routing + restore | `feat/frontend-conversation-ui` | `ConversationSidebar.tsx`, `ConversationItem.tsx`, `DeleteConfirmDialog.tsx` (new), `App.tsx`, `ChatThread.tsx`, `AnswerCard.tsx`, `main.tsx`, `styles.css` (modify) | none | Vitest conversation UI |
| 2.7 Stale-pending repair + startup sweep | `feat/conversation-pending-repair` | `conversation_service.py`, `app/startup.py` (new) | none | `test_messages_failure` extension |

**Complexity:** XL (largest phase). **Risks:** message status bugs; cross-user leaks.
**DoD:** acceptance criteria in `conversation-management-and-persistence-plan.md` §12
(persistence + integrity).

---

## 5. Phase 3 — Multi-turn RAG context

| Task | Branch | Files | Migration | Tests |
| --- | --- | --- | --- | --- |
| 3.1 Context state column | `feat/conversation-context-state` | model (modify) | `b3_conversation_context` | model test |
| 3.2 Deterministic query resolution | `feat/query-resolution-deterministic` | `app/services/conversation_query_service.py` (new) | none | `test_query_resolution` (deterministic cases) |
| 3.3 Constrained LLM rewrite (flagged) | `feat/query-rewrite-llm` | same, `app/core/settings.py` (flag) | none | rewrite validation + fallback tests |
| 3.4 Wire into conversation ask | `feat/conversation-multiturn-ask` | `conversation_service.py` | none | `test_conversation_prompt_isolation`, `test_conversation_rag_guarantees` |

**Complexity:** M. **Risks:** rewrite introduces unsupported claims into retrieval (mitigated:
rewriter sees user questions only; rewrite validated; stored for audit).
**DoD:** follow-up scenario in the brief works; `rag_service.py` unmodified.

---

## 6. Phase 4 — Incident management and vector synchronisation

| Task | Branch | Files | Migration | Tests |
| --- | --- | --- | --- | --- |
| 4.1 Incident read API (list/get) | `feat/incidents-read-api` | `app/api/incidents.py`, `app/schemas/incident.py` (new), `incident_repository.py` (modify, additive) | none | `test_incidents_api` |
| 4.2 Schema: version/timestamps/archived_at/FKs | `feat/incidents-schema-hardening` | models (modify) | `c4_incident_management` | migration test |
| 4.3 Update (optimistic concurrency) | `feat/incidents-update` | `app/services/incident_write_service.py` (new), `incidents.py` | none | `test_incidents_update` |
| 4.4 Archive (soft delete) | `feat/incidents-archive` | same | none | `test_incidents_archive_delete` |
| 4.5 Retrieval excludes archived | `feat/rag-exclude-archived` | `rag_service._collect_evidence` (modify), `incident_repository.get_resolved_incidents` (modify) | none | `test_rag_after_update` extension |
| 4.6 Index jobs + vector upsert/delete | `feat/index-jobs` | `app/models/index_job.py`, `app/services/index_job_service.py` (new), `vector_store_service.py` (additive) | `c5_index_jobs` | `test_index_jobs` |
| 4.7 Reconcile tool + admin endpoint | `feat/vector-reconcile` | `vector_store_service.py`, `app/api/admin_index.py` (new) | none | `test_vector_reconcile` |
| 4.8 Audit rows for mutations | `feat/incident-audit` | `incident_write_service.py` | none | audit assertions |

**Migration sequence:** `c4_incident_management` → `c5_index_jobs`.
**Complexity:** L. **Risks:** modifying the RAG read path (keep to one predicate + tests);
FK constraint change.
**DoD:** failure-mode matrix in `incident-management-and-rag-evolution-plan.md` §3.3 is
covered by tests.

---

## 7. Phase 5 — Streaming

| Task | Branch | Files | Migration |
| --- | --- | --- | --- |
| 5.1 Async Groq stream bridge | `feat/streaming-backend` | `app/services/groq_service.py` (additive function), `conversation_service.py` | none |
| 5.2 SSE endpoint + persistence | `feat/streaming-sse-endpoint` | `app/api/conversations.py` (modify) | none |
| 5.3 Cancellation | `feat/streaming-cancel` | conversation service/route | none |
| 5.4 Frontend reader + cancel | `feat/frontend-streaming` | `conversationClient.ts`, `useConversation.ts`, `ChatThread.tsx`, `AnswerCard.tsx` | none |

**Complexity:** M. **Risks:** thread/async bridging; proxy buffering. **DoD:** streaming
acceptance criteria.

---

## 8. Phase 6 — Production readiness

| Task | Branch | Scope |
| --- | --- | --- |
| 6.1 Lint + type gates | `chore/lint-type-gates` | `ruff`, `mypy` config; fix findings in a bounded set |
| 6.2 Backend CI workflow | `chore/ci-backend` | `ci.yml`; includes Postgres service for concurrency tests + migration check |
| 6.3 Retrieval eval in CI | `chore/ci-retrieval-eval` | artifact + thresholds |
| 6.4 Frontend test runner | `chore/frontend-vitest` | Vitest + RTL + MSW; tests per §15 of readiness plan |
| 6.5 Import hook extraction | `refactor/import-state-machine` | move logic from `ImportDialog.tsx` into `useIncidentImport.ts`; behaviour unchanged; tested |
| 6.6 Docker images + compose profile | `chore/docker-images` | `backend/Dockerfile`, `frontend/Dockerfile`, `frontend/nginx.conf`, compose prod profile |
| 6.7 Readiness + observability | `feat/observability` | `/ready`, structured logs, request id, metrics |
| 6.8 Backup/restore runbook | `docs/backup-restore-runbook` | documentation only |

**Complexity:** L. **Risks:** CI time/cost; flaky timing tests.

---

## 9. Phase 7 — Advanced RAG

| Task | Branch | Scope |
| --- | --- | --- |
| 7.1 Held-out dataset | `eval/heldout-dataset` | new JSON split, human-verified |
| 7.2 Harness split support | `eval/retrieval-split-support` | CLI flag; no metric change |
| 7.3 Machine autocomplete | `feat/machine-autocomplete` | backend endpoint + composer typeahead |
| 7.4 Hybrid retriever (flagged) | `feat/hybrid-retrieval` | BM25 + RRF behind `RETRIEVAL_MODE` |
| 7.5 Threshold re-calibration record | `docs/retrieval-calibration` | decision record with heldout numbers |
| 7.6 OCR (if approved) | `feat/pdf-ocr` | PDFs without text layer; disclaimers |

**Complexity:** L. **Risks:** overfitting; latency. **DoD:** promotion gates met on heldout.

---

## 10. Git branch strategy

| Rule | Detail |
| --- | --- |
| Base | `main` is always deployable after Phase 6 |
| Branch naming | `<type>/<short-slug>` — `feat/`, `fix/`, `chore/`, `refactor/`, `eval/`, `docs/` |
| Size | one logical change per branch; no branch mixes phases |
| Reviews | one reviewer minimum; two for migrations and `rag_service.py` changes |
| Migrations | one revision per branch; never edit an applied revision |
| Merge | squash-merge with the branch title as the commit subject |
| Feature flags | long-lived branches avoided by flags (`RETRIEVAL_MODE`, `QUERY_REWRITE_ENABLED`) |

---

## 11. Files to create / modify by phase (summary)

**Create (new files only):**

- `app/core/settings.py`, `app/core/security.py`
- `app/models/{user,role,refresh_session,audit_log,conversation,message,index_job}.py`
- `app/api/{auth,admin_users,conversations,incidents,admin_index}.py`
- `app/schemas/{auth,conversation,incident}.py`
- `app/dependencies/auth.py`
- `app/middleware/request_id.py`
- `app/services/{auth_service,user_admin_service,audit_service,conversation_service,conversation_query_service,incident_write_service,index_job_service}.py`
- `app/repositories/conversation_repository.py`
- `app/scripts/{create_admin,process_index_jobs}.py`
- `app/startup.py`
- migrations: `a1_users_roles`, `a2_audit_log`, `b2_conversations_messages`, `b3_conversation_context`, `c4_incident_management`, `c5_index_jobs`
- `tests/test_auth_*.py`, `test_admin_users.py`, `test_audit_*.py`, `test_conversations_*.py`, `test_messages_*.py`, `test_incidents_*.py`, `test_index_jobs.py`, `test_vector_reconcile.py`, `test_query_resolution.py`
- frontend: `src/auth/*`, `src/pages/LoginPage.tsx`, `src/api/{authClient,conversationClient}.ts`, `src/hooks/{useConversations,useConversation}.ts`, `src/components/{ConversationSidebar,ConversationItem,DeleteConfirmDialog}.tsx`, `src/test/*`
- infra: `.github/workflows/{ci,docker,nightly}.yml`, `backend/Dockerfile`, `frontend/Dockerfile`, `frontend/nginx.conf`, `.env.example`

**Modify (reviewed changes only):**

- `app/api/main.py` (include routers; protect routes)
- `app/api/incident_import.py` (auth dependencies only)
- `app/models/__init__.py`, `app/repositories/incident_repository.py` (additive)
- `app/services/rag_service.py` (**one condition** for archived incidents in `_collect_evidence`; nothing else)
- `app/services/vector_store_service.py` (additive functions)
- `app/services/groq_service.py` (additive streaming function)
- `app/db/database.py` (**optional**: rollback in `get_db` finally) — flagged for review
- `compose.yaml` (add services in prod profile)
- `frontend/src/{App,main}.tsx`, `types.ts`, `api/client.ts`, `components/{ChatThread,AnswerCard,Composer,Header,ImportDialog}.tsx`, `hooks/useAsk.ts`, `styles.css`, `package.json`
- `README.md`, `.gitignore`

**Not modified without explicit approval:** `backend/app/main.py` (legacy), existing tests
(except adding auth fixtures as described).

---

## 12. Database migration sequence

| # | Revision | Tables / columns | Phase | Downgrade |
| --- | --- | --- | --- | --- |
| 1 | `a1_users_roles` | users, roles, user_roles, refresh_sessions, password_reset_tokens; seed roles | 1 | drop tables |
| 2 | `a2_audit_log` | audit_log | 1 | drop table |
| 3 | `b2_conversations_messages` | conversations, conversation_messages | 2 | drop tables |
| 4 | `b3_conversation_context` | conversations.context_state, messages.rewritten_query | 3 | drop columns |
| 5 | `c4_incident_management` | incident timestamps/version/archived_at/updated_by; indexes; FK ondelete | 4 | drop columns/indexes |
| 6 | `c5_index_jobs` | index_jobs | 4 | drop table |

Rules: every migration is forward-only in production; each ships with a tested `downgrade`;
`alembic check` must report no drift in CI.

---

## 13. Complexity estimates

| Phase | Complexity | Rough effort | Main uncertainty |
| --- | --- | --- | --- |
| 0 | XS | 1–2 days | decisions |
| 1 | L | 2–3 weeks | session/token edge cases |
| 2 | XL | 3–4 weeks | UI rewrite + persistence correctness |
| 3 | M | 1–2 weeks | rewrite quality |
| 4 | L | 2–3 weeks | vector sync + concurrency |
| 5 | M | 1–2 weeks | async/threading |
| 6 | L | 2–3 weeks | CI stability |
| 7 | L | 3–4 weeks | eval dataset quality |

Estimates assume one experienced full-stack engineer. **No estimate includes review latency.**

---

## 14. Risks and mitigation (program level)

| Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- |
| Existing tests break on auth | High | High | shared auth fixture; explicit 401 tests; no assertion weakening |
| Conversation phase expands unboundedly | Medium | High | strict DoD; defer niceties (search, pin) |
| Chroma multi-process corruption | Medium | High | single API process; job lock |
| Stored AI text leaks into evidence | Low | High | rewriter sees user questions only; tests assert evidence JSON |
| Hybrid retrieval overfits | High if unmanaged | Medium | held-out gate; no promotion on dev |
| Scope creep into document KB | Medium | Medium | explicitly out of scope |
| Streaming adds instability | Medium | Medium | feature flag; stateless path retained |
| Migration failure in production | Low | High | tested downgrades; backup before deploy |

---

## 15. Definition of Done (applies to every branch)

- [ ] Acceptance criteria of its plan section met.
- [ ] New tests written first or alongside; all existing tests pass unmodified in assertions.
- [ ] No secrets, prompts or document text in logs or audit metadata.
- [ ] Authorization enforced server-side for every new route.
- [ ] Migration (if any) has a working `downgrade` and passes `alembic check`.
- [ ] `README.md` updated for user-visible behaviour changes.
- [ ] No unrelated refactors bundled in.
- [ ] `rag_service` grounding invariants still proven by tests.

---

## 16. Milestones and release strategy

| Milestone | Contents | Releasable outcome |
| --- | --- | --- |
| M1 — Secure access | Phases 0–1 | Authenticated, role-gated platform; import protected |
| M2 — Persistent assistant | Phases 2–3 | Users have conversation history; follow-ups work |
| M3 — Managed knowledge | Phase 4 | Incidents editable with audit; vector self-healing |
| M4 — Responsive assistant | Phase 5 | Streaming answers with cancel |
| M5 — Operable | Phase 6 | CI/CD, containers, tests, HTTPS deployment |
| M6 — Measured quality | Phase 7 | Held-out eval and, if justified, hybrid retrieval |

Release policy: each milestone is a minor version (`0.x → 1.0` at M5). Semantic versioning
starts at **1.0.0** when M5 completes, since the API is stable, secure and containerised.

---

## 17. Open decisions that block scheduling

1. Engineer import permission (Phase 1).
2. Archive-only vs hard delete (Phase 4).
3. Email delivery for password reset (Phase 1 scope).
4. Hosting target and observability vendor (Phase 6).
5. OCR approval (Phase 7).
6. Held-out dataset ownership (Phase 7).
7. Whether to retire the legacy Phase 1 app (dedicated cleanup branch, after M5).