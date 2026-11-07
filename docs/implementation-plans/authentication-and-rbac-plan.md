# MachineCare AI — Authentication and RBAC Plan (Phase 1)

> Planning only. Nothing in this document is implemented. See
> `enterprise-architecture-assessment.md` for the legend and verified baseline.

---

## 1. Objectives

1. Establish a **user identity** that every other phase depends on.
2. Enforce **role-based authorization server-side** on every endpoint, including the
   currently unauthenticated incident import and reindex routes.
3. Do it **without breaking the existing RAG pipeline**, its tests, or the deferred-import
   pattern in `app/api/main.py`.
4. Produce an audit trail from day one rather than retrofitting it.

Non-goals for this phase: SSO/OIDC, MFA, self-service registration, organization tenancy,
external identity providers. These are explicitly out of scope and listed as future work.

---

## 2. Current state (verified)

| Fact | Location |
| --- | --- |
| No users, roles, tokens or session code exists | repository-wide search |
| All routers are mounted on one app | `app/api/main.py:95-102` |
| Database config is read at import time from `DB_*` | `app/db/database.py:16-21` |
| DB, models and vector store are imported lazily inside handlers | `app/api/main.py:51-70`, `incident_import.py` docstring |
| Sessions are synchronous SQLAlchemy; dependency is `get_db` | `app/db/database.py:31-38` |
| Tests override FastAPI dependencies | `tests/test_api.py`, `tests/conftest.py` |
| Frontend has no router and no auth context | `frontend/src/main.tsx`, `App.tsx` |

---

## 3. Authentication architecture

### 3.1 Recommended model

**Admin-provisioned accounts with server-issued tokens.**

```
POST /api/v1/auth/login  ──> { access_token (JWT, 15 min) }  + Set-Cookie: refresh (HttpOnly, Secure, SameSite=Strict, 14 d)
POST /api/v1/auth/refresh ─> new access token, refresh token rotated
POST /api/v1/auth/logout  ─> revoke current refresh session, clear cookie
GET  /api/v1/auth/me      ─> current principal + permissions
POST /api/v1/auth/change-password  (self)
Admin: POST /api/v1/admin/users, PATCH .../users/{id}, POST .../users/{id}/reset-password,
       POST .../users/{id}/disable, GET .../users, GET .../roles
```

**[RECOMMENDATION]** Access token in **memory only** on the frontend; refresh token in an
**HttpOnly cookie**. Do not use `localStorage` for either.

Comparison of storage options:

| Option | XSS exposure | CSRF exposure | Revocation | Verdict |
| --- | --- | --- | --- | --- |
| Access + refresh in `localStorage` | high (both readable) | none (no auto-send) | hard | **reject** |
| Both in HttpOnly cookies | low | needs CSRF token / SameSite | good | acceptable fallback |
| **Access in memory, refresh in HttpOnly cookie** | low | only refresh path needs SameSite=Strict | good (refresh sessions revocable) | **recommended** |
| In-memory only, re-login on refresh | low | none | perfect | worse UX, rejected |

This choice is compatible with the current Vite dev proxy because the cookie is same-origin
(`localhost:5173` → proxied to `:8001`); in production the reverse proxy must serve the SPA
and the API on the **same origin**.

### 3.2 Token strategy

| Property | Access token | Refresh token |
| --- | --- | --- |
| Format | JWT HS256 (single deployment) or RS256 if multi-service | opaque 256-bit random |
| Lifetime | **15 minutes** | **14 days**, idle-revocable |
| Claims | `sub` (user id), `sid` (session id), `roles`, `iat`, `exp`, `iss`, `aud`, `jti` | none (stored hashed) |
| Storage | client memory | HttpOnly cookie + `refresh_sessions` table |
| Revocation | natural expiry (15 min) + `token_version` claim check | row deleted on logout/reset/disable |
| Rotation | new token on refresh | rotated on every use; **reuse detection revokes the family** |

**[RECOMMENDATION]** Include `token_version` (per user) in the access token and compare it
with `users.token_version` on each request. Incrementing it (password change, role change,
disable) instantly invalidates every outstanding access token. This closes the "15-minute
window after demotion" gap that plain short-lived JWTs have.

**[OPEN]** HS256 vs RS256: choose HS256 for a single-process deployment; RS256 only if the
API is ever split.

### 3.3 Session and login-failure handling

| Concern | Design |
| --- | --- |
| Session tracking | `refresh_sessions(id, user_id, token_hash, issued_at, expires_at, rotated_from, revoked_at, user_agent, ip_hash)` |
| Login failures | per-account counter + exponential lockout (5 fails → 1 min, doubling, cap 15 min) stored in `users.failed_login_count`, `users.locked_until` |
| Timing attacks | always compare against a dummy Argon2 hash when the user is unknown |
| Generic errors | `401 Invalid credentials` for unknown user **and** wrong password |
| Logout | revoke that session only; "logout everywhere" revokes all sessions and bumps `token_version` |
| Cookie flags | `HttpOnly`, `Secure` (required in production), `SameSite=Strict`, `Path=/api/v1/auth` |
| CSRF | refresh endpoint requires `SameSite=Strict` cookie only (no state-changing GETs); if any cross-site need appears, add double-submit token |
| HTTPS | **required** in production; `Secure` cookie and HSTS assumed behind the reverse proxy (Phase 7) |

### 3.4 Password reset (admin-driven)

- Admin triggers `POST /api/v1/admin/users/{id}/reset-password` → server generates a
  one-time reset token (hashed, 30 min TTL, single use) recorded in `password_reset_tokens`.
- **[RECOMMENDATION]** In v1 there is **no email delivery**; the admin receives the one-time
  token in the API response and hands it over out-of-band. This avoids adding an email
  provider in Phase 1. **[OPEN]** if email is required, that becomes an additional
  integration and moves to Phase 6.
- The user sets a new password at `POST /api/v1/auth/reset-password`, which bumps
  `token_version` and revokes all refresh sessions.

### 3.5 Password security

| Rule | Value |
| --- | --- |
| Hash | **Argon2id** via `pwdlib[argon2]` (recommended) or `argon2-cffi` |
| Parameters | library defaults, benchmarked to ~50–100 ms on target hardware |
| Minimum policy | length ≥ 12, no composition rules, reject the top-10k common passwords (bundled list), reject the username |
| Rehash on login | if the stored hash parameters are below current defaults |
| Never logged | passwords, hashes, tokens, reset tokens |

**[VERIFIED]** No hashing library is currently installed. Adding one is an
approval-gated dependency change.

---

## 4. User and role database design

### 4.1 Proposed schema (new Alembic revision `a1_users_roles`)

```
users
  id                BIGINT PK (identity)
  username          VARCHAR(64)  UNIQUE NOT NULL      -- case-insensitive via lower() unique index
  display_name      VARCHAR(150) NOT NULL
  password_hash     TEXT         NOT NULL
  is_active         BOOLEAN      NOT NULL DEFAULT true
  token_version     INTEGER      NOT NULL DEFAULT 0
  failed_login_count INTEGER     NOT NULL DEFAULT 0
  locked_until      TIMESTAMPTZ  NULL
  created_at        TIMESTAMPTZ  NOT NULL
  updated_at        TIMESTAMPTZ  NOT NULL
  last_login_at     TIMESTAMPTZ  NULL
  created_by        BIGINT       NULL FK users.id

roles
  id     SMALLINT PK
  name   VARCHAR(32) UNIQUE NOT NULL   -- 'admin' | 'maintenance_engineer' | 'viewer'
  description TEXT

user_roles
  user_id  BIGINT FK users.id  ON DELETE CASCADE
  role_id  SMALLINT FK roles.id
  PK (user_id, role_id)

refresh_sessions
  id            UUID PK
  user_id       BIGINT FK users.id ON DELETE CASCADE
  token_hash    CHAR(64) NOT NULL UNIQUE      -- SHA-256 of opaque token
  issued_at     TIMESTAMPTZ NOT NULL
  expires_at    TIMESTAMPTZ NOT NULL
  rotated_from  UUID NULL
  revoked_at    TIMESTAMPTZ NULL
  user_agent    VARCHAR(255) NULL
  ip_hash       CHAR(64) NULL                 -- never store raw IP

password_reset_tokens
  id          UUID PK
  user_id     BIGINT FK users.id ON DELETE CASCADE
  token_hash  CHAR(64) NOT NULL UNIQUE
  expires_at  TIMESTAMPTZ NOT NULL
  used_at     TIMESTAMPTZ NULL
```

Design notes:

- **[RECOMMENDATION]** Roles are seeded rows (`admin`, `maintenance_engineer`, `viewer`);
  permissions are **code constants**, not a table, in v1. A permission table is overkill for
  three fixed roles and adds joins to every request.
- **[RECOMMENDATION]** `user_roles` is many-to-many even though v1 assigns one role, so a
  user can hold multiple roles later without a migration.
- **[RECOMMENDATION]** Do **not** store raw IP addresses (privacy); store a salted hash if
  correlation is needed for abuse detection.
- **Bootstrap**: a migration seeds the three roles; the first admin is created by a
  one-time CLI (`python -m app.scripts.create_admin`) reading credentials from env/STDIN —
  **not** by a default password in a migration.

### 4.2 Permission model

Permission codes (server-side constants):

| Code | Meaning |
| --- | --- |
| `assistant:use` | call `/api/v1/ask`, `/api/v1/ask-with-files` and conversation endpoints |
| `incidents:read` | list/view incidents |
| `incidents:import` | preview/commit imports |
| `incidents:write` | create/update incidents |
| `incidents:delete` | delete/archive incidents |
| `index:run` | trigger reindex |
| `users:manage` | manage accounts and roles |
| `audit:read` | read the audit log |
| `conversations:manage_own` | implicit for all authenticated users |

### 4.3 Permission matrix

| Permission | Admin | Maintenance Engineer | Viewer |
| --- | --- | --- | --- |
| `assistant:use` | ✅ | ✅ | ✅ |
| `incidents:read` | ✅ | ✅ | ✅ |
| `incidents:import` | ✅ | ✅ *(open decision)* | ❌ |
| `incidents:write` | ✅ | ❌ | ❌ |
| `incidents:delete` | ✅ | ❌ | ❌ |
| `index:run` | ✅ | ❌ | ❌ |
| `users:manage` | ✅ | ❌ | ❌ |
| `audit:read` | ✅ | ❌ | ❌ |

**[OPEN]** The brief lists Engineer import as "if permitted". Recommendation: **yes**,
because import is an append-only bulk capture workflow that engineers already perform
offline; it never updates or deletes. If the organization wants tighter control, flip this
one cell and nothing else changes.

**[ASSUMPTION]** "View all permitted incidents" for Admin implies full read; there is no
per-line tenancy in v1. If line-level scoping is required later, add `machine_scope` to
`user_roles` and filter in `incidents:read` queries — design note only.

---

## 5. API endpoint protection

### 5.1 Complete endpoint inventory and required permissions (post-Phase-1)

| Method | Path | Auth | Permission | Notes |
| --- | --- | --- | --- | --- |
| GET | `/health` | **public** | — | liveness only; must stay dependency-free |
| GET | `/ready` *(new, Phase 7)* | public | — | DB/Chroma/Groq readiness |
| POST | `/api/v1/auth/login` | public | — | rate-limited |
| POST | `/api/v1/auth/refresh` | refresh cookie | — | |
| POST | `/api/v1/auth/logout` | access token | — | |
| GET | `/api/v1/auth/me` | access token | — | returns roles + permissions |
| POST | `/api/v1/auth/change-password` | access token | — | bumps token_version |
| POST | `/api/v1/auth/reset-password` | reset token | — | |
| POST | `/api/v1/ask` | access token | `assistant:use` | **changes behavior** |
| POST | `/api/v1/ask-with-files` | access token | `assistant:use` | **changes behavior** |
| GET | `/api/v1/incidents/import/template.xlsx` | access token | `incidents:import` | |
| GET | `/api/v1/incidents/import/machines` | access token | `incidents:read` or `incidents:import` | used by composer autocomplete later |
| POST | `/api/v1/incidents/import/preview` | access token | `incidents:import` | |
| POST | `/api/v1/incidents/import` | access token | `incidents:import` | write path |
| POST | `/api/v1/incidents/import/reindex` | access token | `index:run` | **moved to admin** |
| GET | `/api/v1/incidents` *(new, Phase 4)* | access token | `incidents:read` | drill-down (DR-1) |
| GET | `/api/v1/incidents/{incident_id}` *(new, Phase 4)* | access token | `incidents:read` | drill-down |
| PATCH | `/api/v1/incidents/{incident_id}` *(new, Phase 4)* | access token | `incidents:write` | |
| DELETE | `/api/v1/incidents/{incident_id}` *(new, Phase 4)* | access token | `incidents:delete` | archive |
| GET/POST/PATCH/DELETE | `/api/v1/conversations*` *(new, Phase 2)* | access token | `assistant:use` | ownership enforced in queries |
| GET | `/api/v1/audit` *(new, Phase 6)* | access token | `audit:read` | |
| GET/POST/PATCH | `/api/v1/admin/users*` *(new, Phase 1)* | access token | `users:manage` | |
| GET | `/api/v1/admin/roles` *(new, Phase 1)* | access token | `users:manage` | |

Note there is **no existing reindex-for-engineers** route; the current
`POST /api/v1/incidents/import/reindex` is granted to Admin only under this plan, because it
rewrites the whole vector collection.

### 5.2 HTTP semantics

| Situation | Status | Body |
| --- | --- | --- |
| Missing/invalid/expired access token | **401** | `{"detail": "Not authenticated."}` + `WWW-Authenticate: Bearer` |
| Valid token, insufficient permission | **403** | `{"detail": "You do not have permission to perform this action."}` |
| Locked account / bad credentials at login | **401** | `{"detail": "Invalid credentials."}` (same message for both) |
| Revoked refresh session | **401** | `{"detail": "Session expired."}` |
| Disabled user with a valid access token | **401** (treated as unauthenticated) | triggers refresh attempt, then logout |
| Token version mismatch | **401** | as above |

**Important:** 401 vs 403 must be consistent and tested. Existing error handling
(`api/main.py:129-135`) wraps unexpected exceptions in 500; auth dependencies must raise
`HTTPException` directly and never be caught by that wrapper.

### 5.3 Implementation shape

Recommended dependency chain (new module `app/dependencies/auth.py`):

```
get_current_user(token = Depends(bearer_scheme), db = Depends(get_db))
    -> decodes JWT, loads user, checks is_active and token_version, returns Principal
require_permission("incidents:import")
    -> depends on get_current_user, checks the code, else 403
```

Applied per-route (`dependencies=[Depends(require_permission(...))]`) or at router level
with per-route overrides. **[RECOMMENDATION]** Apply explicitly per route, because the
import router mixes read-only preview with a write commit, and audit needs to know the
permission that was granted.

**[RECOMMENDATION]** Introduce `app/core/settings.py` (pydantic-settings) with
`JWT_SECRET`, `ACCESS_TTL_MINUTES`, `REFRESH_TTL_DAYS`, `COOKIE_SECURE`. Settings are read
lazily by the auth dependency, **not at import**, to preserve the `/health`-without-DB
guarantee (`app/api/main.py:51-60`).

---

## 6. Frontend authentication

### 6.1 Current state

**[VERIFIED]** `main.tsx` renders `App` under `StrictMode` with no router, no provider, no
token handling. `api/client.ts` sends no headers and uses same-origin paths.

### 6.2 Proposed structure

| File | Action | Purpose |
| --- | --- | --- |
| `src/auth/AuthProvider.tsx` | **new** | holds access token in memory, exposes `login/logout/refresh`, bootstraps via `/auth/refresh` |
| `src/auth/useAuth.ts` | **new** | context hook |
| `src/api/authClient.ts` | **new** | login/refresh/logout/me calls with `credentials: "include"` |
| `src/api/client.ts` | **modify** | attach `Authorization: Bearer`; on 401 attempt one refresh then retry; emit `onUnauthorized` |
| `src/pages/LoginPage.tsx` | **new** | login form |
| `src/App.tsx` | **modify** | wrap in provider + route guard; keep chat as the root route |
| `src/components/Header.tsx` | **modify** | show user name, role, logout |
| `package.json` | **modify** | add `react-router-dom` |

**[RECOMMENDATION]** Do not gate features only in the UI. The import button and future
admin pages hide based on `permissions` from `/auth/me`, but the server is authoritative.

### 6.3 Token handling rules

- Access token is a React state value in `AuthProvider`, never written to storage.
- On app load: call `POST /auth/refresh` once (cookie sent automatically); success → set
  access token and load `/auth/me`.
- On any `401`: attempt refresh once; if that fails, clear state and route to `/login`.
- Concurrent 401s share a single in-flight refresh promise.
- Logout clears state, calls `/auth/logout`, navigates to `/login`.
- The existing `useAsk` retry path does not need changes; it will surface 401 as an error
  only if refresh fails.

---

## 7. Audit logging (Phase 1 subset; full plan in Phase 6)

Phase 1 writes audit events for authentication and user administration so Phase 6 only
extends the schema consumers.

```
audit_log
  id             BIGINT PK
  occurred_at    TIMESTAMPTZ NOT NULL
  actor_user_id  BIGINT NULL FK users.id      -- NULL for failed login of unknown user
  actor_username VARCHAR(64) NULL             -- captured at attempt time
  action         VARCHAR(64) NOT NULL         -- e.g. auth.login.failed, user.role.changed
  resource_type  VARCHAR(64) NULL
  resource_id    VARCHAR(128) NULL
  request_id     CHAR(36) NULL                -- correlation id from middleware
  ip_hash        CHAR(64) NULL
  metadata       JSONB NOT NULL DEFAULT '{}'  -- never contains secrets
  outcome        VARCHAR(16) NOT NULL         -- success | failure
```

Actions logged in Phase 1: `auth.login.succeeded`, `auth.login.failed`,
`auth.login.locked`, `auth.logout`, `auth.refresh.reuse_detected`,
`auth.password.changed`, `auth.password.reset`, `user.created`, `user.updated`,
`user.disabled`, `user.role.changed`.

**[RECOMMENDATION]** Write audit rows **in the same transaction as the mutation** for
administrative changes; write authentication events with their own transaction (they have
no mutation to roll back). Failed logins must still be recorded, so never let an audit
failure block a legitimate login — log and continue for auth events, fail closed for
administrative mutations.

---

## 8. Security tests

New test modules (backend, `tests/`):

| Module | Cases |
| --- | --- |
| `test_auth_password.py` | Argon2 hash/verify; parameter upgrade; policy rejects short/common/username passwords |
| `test_auth_login.py` | valid login; wrong password; unknown user; identical error message; lockout after N failures; unlock after TTL |
| `test_auth_tokens.py` | valid access; expired access; malformed; wrong signature; wrong issuer/audience; missing token; token_version mismatch |
| `test_auth_refresh.py` | refresh rotates; reuse of old token revokes family; revoked session rejected; logout revokes only that session |
| `test_auth_rbac.py` | matrix enforced per role; 403 body; 401 body; direct API calls bypassing UI |
| `test_auth_endpoints_guard.py` | every protected route returns 401 without a token (parameterised over the router table) |
| `test_admin_users.py` | create/list/update/disable; cannot demote the last admin; cannot disable self (open decision) |
| `test_audit_auth.py` | success/failure events written; no secrets in metadata |

**Migration of existing tests (explicit rule):** existing tests that call protected
endpoints (e.g. `tests/test_api.py`, `test_api_ask_with_files.py`,
`test_api_incident_import*.py`) must be updated by **injecting an authenticated principal**
through a shared fixture (e.g. `auth_client(role="admin")` overriding
`get_current_user`), **not** by disabling auth or weakening assertions. The current
anonymous-success assertions become "anonymous → 401" assertions added to
`test_auth_endpoints_guard.py`.

Rate-limit tests for login can be added with a fake clock; no external service needed.

---

## 9. Backward compatibility and blast radius

| Change | Could break | Mitigation |
| --- | --- | --- |
| `/api/v1/ask*` now requires auth | all ask tests, frontend chat | shared auth fixture in tests; login gate in UI |
| Import endpoints require auth | 67 existing import tests | auth fixture; assert 401 without fixture |
| `/reindex` becomes admin-only | frontend retry-index flow for engineers | UI hides retry when `index:run` absent; result explains "ask an administrator" |
| New required env vars (`JWT_SECRET`) | app startup if missing | settings validated lazily on first auth call; `/health` still works |
| Login rate limiting | tests hitting login repeatedly | injectable clock/limiter |
| Cookie `Secure` in dev | refresh cookie dropped over http on non-localhost | set `COOKIE_SECURE=false` in dev `.env` |

---

## 10. Acceptance criteria

**Functional**
- [ ] A seeded admin can log in, call `/auth/me`, and use every permitted endpoint.
- [ ] Viewer and Engineer receive 403 on `incidents:write`, `incidents:delete`,
      `users:manage`, `audit:read`, `index:run`; Admin receives 200.
- [ ] Engineer can preview and commit an import (if the `[OPEN]` decision keeps it) and
      cannot call `/reindex`.
- [ ] Every endpoint in §5.1 returns 401 without a token, tested by a parameterised test.
- [ ] Logout revokes the refresh session; a stolen access token stops working after
      `token_version` bumps.

**Security**
- [ ] Passwords are Argon2id; no plaintext or hash ever appears in logs or audit metadata.
- [ ] Login uses constant-time comparison for unknown users.
- [ ] Account locks after the configured number of failures and recovers after TTL.
- [ ] No endpoint trusts a `user_id`, `role` or `is_admin` value supplied by the client.

**Quality**
- [ ] All pre-existing tests pass after adding the auth fixture; no assertion weakened.
- [ ] New auth tests listed in §8 pass on in-memory SQLite.
- [ ] `ruff` and `mypy` pass on new modules (see Phase 6 for gate enforcement).

**Documentation**
- [ ] README §14 updated to state that import/reindex require authentication.
- [ ] `.env.example` (new) lists `JWT_SECRET`, `ACCESS_TTL_MINUTES`,
      `REFRESH_TTL_DAYS`, `COOKIE_SECURE`.

---

## 11. Open questions

1. Engineer import permission (recommended: granted).
2. Should an admin be able to disable their own account? (recommended: no.)
3. Is email-based password reset required in v1? (recommended: no; admin-issued tokens.)
4. Session lifetime for "remember me" — v1 has none (recommended: constant 14 days).
5. Do conversations need per-role data isolation (e.g. Viewer sees only own)? v1: all
   conversations are always private to their owner.