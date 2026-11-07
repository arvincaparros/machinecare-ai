# MachineCare AI — Conversation Management and Persistence Plan (Phases 2–3)

> Planning only. See `enterprise-architecture-assessment.md` for the legend.

---

## 1. Why this phase matters

This is the largest behavioural change in the roadmap. Today the conversation exists only
in `useAsk.ts` state; each question is answered in isolation. Persistence introduces a new
kind of data (user-authored content), a new ownership boundary (cross-user isolation), and
a new risk surface: **stored AI text must never be mistaken for verified historical
evidence**. The design below keeps the existing grounding pipeline untouched and layers
conversation state around it.

---

## 2. Current state (verified)

| Fact | Location |
| --- | --- |
| Transcript is `useState<ChatTurn[]>`; no storage | `frontend/src/hooks/useAsk.ts:35` |
| One in-flight request; abort on unmount/replace | `useAsk.ts:55-56`, `38` |
| Retry reuses question + machine + `File[]` kept on the assistant turn | `useAsk.ts:151-159`, `types.ts` `AssistantTurn.files` |
| Each request sends only `{question, machine}` | `api/client.ts` `askQuestion` |
| Backend has no conversation concept | `app/api/main.py` |
| Response shape `AskResponse` is the unit that would be persisted | `app/api/schemas.py` |
| Attachments are per-question and discarded | `document_extraction.py`, README §10 |
| No frontend router or data-fetching library | `frontend/src/main.tsx` |

---

## 3. Conversation architecture

```
Browser                                  RAG API
ConversationSidebar ─┐                   ┌─ /api/v1/conversations          (CRUD, ownership)
ChatThread ──────────┼─ conversationClient┼─ /api/v1/conversations/{id}/messages (ask)
Composer/useAsk ─────┘                   │
                                         ├─ conversation_service
                                         │    ├─ ownership check (user_id in WHERE)
                                         │    ├─ query resolution (multi-turn, §8)
                                         │    ├─ rag_service.ask_machinecare*  (unchanged)
                                         │    └─ message persistence (status machine)
                                         └─ PostgreSQL: conversations, conversation_messages
```

**[RECOMMENDATION]** Conversation persistence is **server-side**, not localStorage. Reasons:
ownership is a server concern, multi-device use is expected, and Phase 5 streaming needs a
server-side message lifecycle. Client-side caching may be added later via TanStack Query.

---

## 4. PostgreSQL schema

### 4.1 Migration sequence

| Revision (proposed id) | Tables | Phase |
| --- | --- | --- |
| `b2_conversations_messages` | `conversations`, `conversation_messages` | 2 |
| `b3_conversation_context` | `conversations.context_state`, message `rewritten_query` | 3 |

Splitting Phase 2 and Phase 3 into separate revisions keeps the multi-turn feature
releasable after persistence is stable.

### 4.2 `conversations`

```
id           UUID PK (gen_random_uuid())
user_id      BIGINT NOT NULL FK users.id ON DELETE CASCADE
title        VARCHAR(120) NOT NULL                -- derived initially, user-editable
created_at   TIMESTAMPTZ NOT NULL
updated_at   TIMESTAMPTZ NOT NULL                 -- bumped on every message
deleted_at   TIMESTAMPTZ NULL                     -- soft delete
message_count INTEGER NOT NULL DEFAULT 0          -- denormalised for list view
INDEX (user_id, deleted_at, updated_at DESC)      -- list query
INDEX (user_id, created_at DESC)
```

Design decisions:

- **[RECOMMENDATION] Soft delete.** Deleting a conversation is reversible for the user and
  keeps audit references intact. Hard delete can be a periodic retention job (open).
- **[RECOMMENDATION]** `title` auto-generated from the first user message (truncated to 60
  chars, stripped of newlines). Users can rename.
- **[RECOMMENDATION]** `message_count` is maintained transactionally so the sidebar does not
  need a join/aggregate per row.
- **[RECOMMENDATION]** `updated_at` is bumped by the message write in the same transaction;
  ordering is `updated_at DESC, id DESC` for stable pagination.

### 4.3 `conversation_messages`

```
id                 UUID PK
conversation_id    UUID NOT NULL FK conversations.id ON DELETE CASCADE
sequence           INTEGER NOT NULL              -- 1,2,3… per conversation
role               VARCHAR(16) NOT NULL          -- 'user' | 'assistant'
content            TEXT NOT NULL DEFAULT ''
status             VARCHAR(16) NOT NULL          -- see §4.4
machine            VARCHAR(50) NULL              -- machine filter used for retrieval
rewritten_query    TEXT NULL                      -- Phase 3: resolved query for assistant msgs
citations          JSONB NOT NULL DEFAULT '[]'    -- [{incident_id, machine, distance}]
cited_incident_ids JSONB NOT NULL DEFAULT '[]'
unsupported_citations JSONB NOT NULL DEFAULT '[]'
model              VARCHAR(64) NULL               -- e.g. openai/gpt-oss-20b
retrieval          JSONB NULL                     -- {top_k, max_distance, matches_seen, kept, filter}
usage              JSONB NULL                     -- {prompt_tokens, completion_tokens} if provider returns it
attachments        JSONB NOT NULL DEFAULT '[]'    -- [{filename, characters, truncated}]
error_message      TEXT NULL                      -- generic, never a traceback
created_at         TIMESTAMPTZ NOT NULL
completed_at       TIMESTAMPTZ NULL
UNIQUE (conversation_id, sequence)
INDEX (conversation_id, sequence)
```

Design decisions:

- **[RECOMMENDATION]** Keep the existing `AskResponse` field names inside JSONB (`citations`,
  `cited_incident_ids`, `unsupported_citations`) so the frontend can reuse
  `AnswerCard`/`SourcesPanel` without a parallel type.
- **[RECOMMENDATION]** `retrieval` metadata is **justified** because it is needed to explain
  why an answer had fewer sources than usual and to debug threshold behaviour, but it stores
  only counts/distances, **not** the prompt or the full evidence text.
- **[RECOMMENDATION]** Do **not** persist Chroma documents, prompt text, or Groq raw
  responses. Evidence can be re-derived; retaining prompt text would duplicate incident data
  and complicate retention.
- **[RECOMMENDATION]** `attachments` stores **metadata only** — filenames, extracted
  character counts and `truncated` flags (already returned by `AskWithFilesResponse`).
  **File bytes and extracted text are never persisted**, preserving the current privacy
  property. A later re-ask requires re-attaching the file; the UI must say so.
- **[RECOMMENDATION]** No `user_id` column on messages: ownership comes through
  `conversation_id`. This removes any possibility of a message with an inconsistent owner.
  Ownership is enforced in every query by joining through `conversations.user_id`.

### 4.4 Message status machine

```
user message:      'complete'                       (written before generation starts)

assistant message: 'pending'  → 'complete'
                             → 'failed'      (LLM/DB/safety error; error_message set)
                             → 'cancelled'   (user aborted — Phase 5)
                             → 'streaming'   (Phase 5 only; transient)
```

- **[RECOMMENDATION] Persist incrementally, not only on success.** The assistant row is
  inserted with `status='pending'` **before** the LLM call, and updated to the terminal
  status after. This is the only way a refresh during generation shows something honest.
- Until Phase 5, a pending row whose process died is resolved at read time: if
  `status='pending'` and `created_at` is older than `PENDING_STALE_SECONDS` (e.g. 120 s),
  the API returns it as `failed` with "The answer was interrupted." and repairs the row
  lazily. **[RECOMMENDATION]** Also add a startup sweep that marks orphaned `pending` rows
  older than the timeout as `failed`.
- A `failed` assistant message never contains partial model output in v1 (non-streaming), so
  there is nothing unsafe to display.

### 4.5 Request transaction behaviour

A single question involves: insert user msg → insert pending assistant msg → bump
conversation → **commit** → run retrieval + LLM (no DB transaction open) → update assistant
msg → commit.

**[RECOMMENDATION]** Do not hold a database transaction open across the LLM call. Holding it
would pin a connection for up to the 90 s client timeout and risk lock contention.

If the LLM call raises, the assistant row is updated to `failed` in a **new** transaction.
If even that update fails, the row remains `pending` and the stale-pending repair (§4.4)
handles it.

---

## 5. API contracts

Routing follows the existing convention (`/api/v1/...`) and is mounted on the same app.

| Method | Path | Request | Response |
| --- | --- | --- | --- |
| POST | `/api/v1/conversations` | `{}` | `ConversationDetail` (empty, title "New conversation") |
| GET | `/api/v1/conversations` | `?cursor=&limit=20` | `{items: ConversationSummary[], next_cursor}` |
| GET | `/api/v1/conversations/{id}` | — | `ConversationDetail` with messages (paginated by `?before=`) |
| PATCH | `/api/v1/conversations/{id}` | `{title}` | `ConversationSummary` |
| DELETE | `/api/v1/conversations/{id}` | — | 204 (soft delete) |
| POST | `/api/v1/conversations/{id}/messages` | JSON `{question, machine}` | `MessagePair` (user + assistant) |
| POST | `/api/v1/conversations/{id}/messages/with-files` | multipart `question`, `machine`, `files` | `MessagePair` |

Response shapes (draft):

```jsonc
ConversationSummary = { id, title, message_count, created_at, updated_at }
ConversationDetail  = ConversationSummary + { messages: Message[] }
Message = {
  id, role, content, status, created_at, completed_at,
  machine, rewritten_query,
  citations, cited_incident_ids, unsupported_citations,
  documents,          // attachments metadata for user turns / document answers
  error_message
}
MessagePair = { user: Message, assistant: Message }
```

Error semantics:

| Status | When |
| --- | --- |
| 401 | not authenticated |
| 403 | authenticated but lacking `assistant:use` |
| 404 | conversation does not exist **or belongs to another user** (do not disclose existence) |
| 409 | conversation is deleted (soft) |
| 422 | blank question, question > 2000 chars, bad machine length |
| 500 | retrieval/generation failure; assistant row set to `failed` and the response may still be 200 with `status: "failed"` **[OPEN]** |

**[OPEN]** For a generation failure, two options: (a) return 500 and let the client mark its
local turn failed, or (b) return 200 with the persisted `failed` message so the client
renders it consistently and can retry by ID. **Recommendation: (b)**, with a `retry` endpoint
`POST /api/v1/conversations/{id}/messages/{message_id}/retry` that reuses the stored
question, machine and (if still available in the request) files. This avoids duplicating the
failed turn.

### 5.1 Cross-user isolation requirements

- Every repository function takes `user_id` and filters `conversations.user_id == user_id`.
- The 404 (not 403) response for another user's conversation prevents ID probing.
- Tests must attempt direct access with a second user's token for every route.
- No endpoint accepts `user_id` from the client.

---

## 6. Frontend chat history

### 6.1 Files to create / modify

| File | Action | Purpose |
| --- | --- | --- |
| `src/api/conversationClient.ts` | **new** | CRUD + message posting; mirrors `types.ts` |
| `src/types.ts` | **modify** | add `ConversationSummary`, `ConversationDetail`, `Message`, `MessageStatus` |
| `src/hooks/useConversations.ts` | **new** | list/create/rename/delete (TanStack Query) |
| `src/hooks/useConversation.ts` | **new** | load one conversation, submit message, retry |
| `src/hooks/useAsk.ts` | **modify** | becomes conversation-aware or is superseded by `useConversation`; keep retry semantics |
| `src/components/ConversationSidebar.tsx` | **new** | list, New chat, rename, delete |
| `src/components/ConversationItem.tsx` | **new** | title, timestamp, menu |
| `src/components/DeleteConfirmDialog.tsx` | **new** | confirmation |
| `src/components/ChatThread.tsx` | **modify** | render persisted messages and statuses |
| `src/components/AnswerCard.tsx` | **modify** | handle `failed`/`cancelled` and attachment-metadata-only note |
| `src/App.tsx` | **modify** | layout: sidebar + panel + rail; route `/c/:conversationId` |
| `src/styles.css` | **modify** | responsive sidebar (collapsible < 1024 px) |
| `src/main.tsx` | **modify** | add router + QueryClient provider |

### 6.2 Behaviour specification

- **New Chat** creates a conversation immediately (server-side) and routes to it; an empty
  conversation is allowed and appears in the sidebar as "New conversation".
- **Suggested prompts** (Hero) still submit immediately; the first submission creates a
  conversation if none is active and retitles it from the question.
- **Restore after refresh**: route `/c/:id` loads the conversation; `/` loads the most
  recent conversation or the empty hero state.
- **Timestamps** show relative time in the sidebar and absolute time on hover.
- **Delete** requires confirmation; the active conversation routes to the next most recent.
- **Error/retry**: a `failed` assistant message shows the same "Try again" affordance as
  today; retry calls the retry endpoint.
- **Empty state**: hero with suggested prompts, unchanged.
- **Responsive**: sidebar becomes an overlay/drawer under 1024 px.

---

## 7. Multi-turn RAG context (Phase 3)

### 7.1 The problem, stated precisely

`ask_machinecare(question, machine)` retrieves from `question` alone. A follow-up such as
"What was the most common cause?" has no retrievable content. The fix must **not** change
the grounding guarantees: retrieval still returns incident IDs, evidence is still reloaded
from PostgreSQL, citations are still validated, and stored assistant text is still never
evidence.

### 7.2 Options evaluated

| Option | How | Pros | Cons |
| --- | --- | --- | --- |
| A. Send full history to the LLM | append prior turns to the prompt | simplest | does not fix retrieval; bloats prompt; prior AI text may be treated as fact |
| B. Structured conversation state | derive `{machine, topic keywords}` from prior turns, use for retrieval | deterministic, cheap, testable | brittle for pronouns/implicit references |
| C. LLM query rewriting | ask a small model call to rewrite the follow-up into a standalone query | handles natural language well | extra latency/cost; needs grounding (the rewrite must be constrained and never invent incident IDs); non-deterministic |
| **D. B + C, deterministic first** | resolve machine/topic from state; if unresolved and history exists, rewrite with a tightly constrained prompt; log the rewritten query | best compliance with "preserve machine context" and "don't send the whole history" | most work |

**[RECOMMENDATION] Option D**, with a hard rule: **the rewritten query is used only for
retrieval, never as evidence.** It is stored in `rewritten_query` for audit and debugging.

### 7.3 Structured state design

Add to `conversations`:

```
context_state JSONB NOT NULL DEFAULT '{}'
  {
    "machine": "AOI-01" | null,          -- last explicit or unambiguous machine
    "recent_incident_ids": [...],         -- last answer's cited IDs (for "which one…")
    "topic_terms": ["calibration","camera"],  -- bounded keyword set
    "turns_since_machine_set": 3
  }
```

Update rules:

- A user turn with an explicit `machine` sets `context_state.machine`.
- A user turn naming a machine code literally (regex over known machine codes) also sets it.
- A turn that explicitly changes topic is not detectable deterministically; rely on the
  rewrite path.
- `topic_terms` are derived from the user's turn (tokenized, stopworded, top 5) and capped.

**[RECOMMENDATION]** Never let `context_state` contain free-form assistant prose. It holds
identifiers and short keywords only, so it cannot become a channel for unverified claims.

### 7.4 Query resolution algorithm

```
resolve_query(new_question, conversation, history):
  1. explicit machine in request            -> use it
  2. machine code mentioned in question     -> use it
  3. context_state.machine and turns_since_machine_set <= MAX_CARRY (default 5)
                                            -> use it (recorded as "inferred")
  4. otherwise                              -> no machine filter
  rewrite:
  a. if question is self-contained (has machine or >= 3 content tokens that are not pronoun-only)
     -> use as-is
  b. else if history is empty -> use as-is
  c. else -> constrained rewrite:
       "Rewrite the follow-up into a standalone maintenance question.
        Use only the conversation context. Do not invent incident IDs,
        machine codes, or causes. Return only the rewritten question."
     inputs: last N user questions (default 3) + resolved machine, NOT assistant answers
  d. validate rewrite: <= 300 chars, no incident IDs present that are not in history,
     no new machine codes; otherwise fall back to as-is
```

**[RECOMMENDATION]** Feed the rewriter **user questions only**, never assistant answers.
This removes the largest path for stored AI text to influence retrieval, and satisfies "never
treat prior AI-generated responses as verified historical facts".

### 7.5 Context window and summarisation

- History sent for rewriting: **last 3 user turns** (bounded tokens).
- No full-history summarisation in v1. **[RECOMMENDATION]** Add it only if measured
  follow-up resolution accuracy on a held-out set is inadequate (ties into Phase 7 eval).
- The answer prompt still receives **only retrieved incident evidence** — unchanged from
  `build_prompt`. The rewritten question replaces the raw follow-up in
  `TECHNICIAN QUESTION`.

### 7.6 Preserving grounding guarantees

| Guarantee | How it survives |
| --- | --- |
| No evidence ⇒ no LLM call | resolution only changes the query string; the gate is unchanged |
| Chroma IDs only | same |
| PostgreSQL revalidation | same `_collect_evidence` |
| Citation enforcement | same `validate_citations` on the new answer |
| Document guard | unchanged |
| No cross-user leakage | history queried by `conversation_id` scoped by `user_id` |
| No AI text as evidence | rewriter sees user questions only; evidence comes from DB |

**[RECOMMENDATION]** Implement resolution in a new `app/services/conversation_query_service.py`.
`rag_service.py` is **not modified**; it continues to accept `(question, machine)`.

---

## 8. Citation and attachment preservation

- Citations, cited IDs and unsupported citations are persisted verbatim from the RAG
  response and returned on reload, so the UI's "verified"/"unverified" markers survive a
  refresh.
- Attachment **metadata** is persisted (filename, characters, truncated); contents are not.
  A reloaded conversation shows "Document: report.pdf (attached, not retained)" and is
  explicit that re-analysis requires re-attaching.
- Images/scanned PDFs are not retained; OCR is Phase 7.

---

## 9. Future streaming compatibility (Phase 5)

The schema above is designed so Phase 5 needs **no migration**:

- `status` already includes `streaming` and `cancelled`.
- Assistant `content` can be updated progressively in the same row.
- `completed_at` set at terminal status.
- A cancelled generation persists `status='cancelled'` with the partial content and a
  `metadata` note; the UI must never label partial text as a completed answer (Phase 5 AC).

Phase 5 will add `POST /api/v1/conversations/{id}/messages/stream` returning SSE frames:

```
event: status   data: {"message_id": "...", "state": "retrieving"}
event: token    data: {"delta": "..."}
event: final    data: {"message": {...Message...}}      // persisted, validated citations
event: error    data: {"message_id": "...", "detail": "generic"}
```

Citations cannot be validated until the full answer exists, so the `final` event carries the
validated message; the client shows a "verifying citations" state between last token and
`final`.

---

## 10. Testing strategy

### 10.1 Backend

| Module | Cases |
| --- | --- |
| `test_conversations_api.py` | create/list/get/rename/delete; pagination; deleted conversation 409/404 |
| `test_conversation_ownership.py` | user B gets 404 on user A's conversation for every route; list never includes others |
| `test_messages_persistence.py` | user+assistant rows, sequence, status transitions, conversation `updated_at` bump, `message_count` |
| `test_messages_failure.py` | LLM raises → assistant `failed` with generic error; DB update failure path; stale `pending` repair |
| `test_attachments_metadata_only.py` | attachment metadata stored; no bytes/text columns written; response omits contents |
| `test_conversation_prompt_isolation.py` | retrieval called with resolved query; prompt evidence contains only DB records; history never appears in evidence JSON |
| `test_query_resolution.py` | machine carry-over; reset after MAX_CARRY; rewrite validation rejects invented IDs; fallback to as-is; deterministic cases need no LLM |
| `test_conversation_citations.py` | citations/unsupported persisted and returned identically after reload |
| `test_conversation_rag_guarantees.py` | no evidence ⇒ no LLM call even with history; grounding prompt unchanged |

Fixtures reuse `db_session_factory`, `add_incident`, `mock_search`, `mock_groq` from
`conftest.py`; add `auth_client` and `conversation_factory`.

### 10.2 Frontend (Phase 6 runner)

- Sidebar lists, selects, renames, deletes (with confirm).
- Refresh restores active conversation and messages.
- Failed answer renders retry; retry hits the retry endpoint.
- Attachment shows metadata-only warning after reload.
- 401 during load routes to login (needs Phase 1 auth context).

---

## 11. Backward compatibility and blast radius

| Change | Effect | Mitigation |
| --- | --- | --- |
| New tables only | no existing table altered | safe |
| `/api/v1/ask` behaviour | retained unchanged for compatibility; conversation endpoints wrap it | keep both during transition |
| `useAsk` replaced | chat UI rewrite | keep `askQuestion` client until conversation client is verified |
| `App.tsx` layout | sidebar added | responsive tests |
| `ChatTurn` types | extended | `AnswerCard` and `SourcesPanel` props stay compatible |

**[RECOMMENDATION]** Keep the stateless `/api/v1/ask` endpoints in production for API
clients and smoke scripts (`backend/test_rag.py`), even after the UI uses conversations.

---

## 12. Acceptance criteria

**Persistence**
- [ ] A conversation survives a full page refresh and shows identical messages, statuses,
      citations and attached-document metadata.
- [ ] Conversations sort by recent activity; rename and soft delete work.
- [ ] A user cannot read, rename, delete, or post to another user's conversation (404).

**Integrity**
- [ ] Every assistant message has a terminal status or is repaired by the stale rule.
- [ ] A generation failure never leaves a `pending` row after the timeout window.
- [ ] No attachment bytes or extracted text are stored in any column (asserted by tests).

**Multi-turn RAG**
- [ ] "Why did AOI-01 stop?" followed by "What was the most common cause?" retrieves
      AOI-01 evidence under the machine context, and every historical claim is still cited.
- [ ] A follow-up with no evidence still returns the fixed no-evidence answer without an
      LLM call.
- [ ] Stored assistant answers never appear inside the evidence JSON sent to the LLM.
- [ ] The rewritten query is stored and visible in the message metadata.

**Quality**
- [ ] Existing `rag_service` tests unchanged and passing.
- [ ] New tests from §10 pass.

---

## 13. Open questions

1. Should an empty conversation be created eagerly (New Chat) or lazily on first message?
   (recommended: eagerly, simplifies routing.)
2. Assistant-message failure: 200 with `failed` message vs 500 (recommended: 200).
3. Retention of soft-deleted conversations (recommended: 30 days, then hard delete — needs
   a job; defer to Phase 6).
4. Do we allow editing message text? (recommended: no.)
5. Should `recent_incident_ids` drive a follow-up like "show me the second one"? (Deferred;
   design note only.)