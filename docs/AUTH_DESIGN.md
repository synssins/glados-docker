# WebUI Authentication — Rebuild Design

**Status:** Draft v5 for operator review
**Author:** Claude (research sessions 2026-04-24)
**Scope:** Research + architecture proposal only. No code changes land
until the operator approves this document.
**Repo state read:** commit `e8cb13f`, `CLAUDE.md`, `docs/SELF_CONTAINMENT.md`,
`docs/roadmap.md`, `glados/webui/tts_ui.py`, `glados/core/config_store.py`,
`glados/tools/set_password.py`, `configs/config.example.yaml`.

**Decisions locked in across v1 → v3 reviews (2026-04-24):**

| Decision | Value |
|---|---|
| Route tiers | **TTS + STT public**; **Chat = any authed user**; **Configuration = admin only** |
| Roles | **`admin`** and **`chat`** only. No `viewer`, no custom roles, no self-signup. |
| `chat` role capability | *Only* the chat tab (home control via GLaDOS). No memory, no logs, no TTS-generator page inside the SPA shell, no settings. |
| User creation | Admin-only via the Users page. Default role in the add-user form is `chat`; dropdown offers `chat` / `admin`. |
| Username | Case-sensitive exact match against whatever the admin typed at add-user time. No canonicalization. Validation: non-empty, ≤ 64 chars, no control characters. |
| Minimum password length | 8 characters. |
| Password denylist | 20-item static list (`password`, `12345678`, `qwerty`, etc.). |
| Password hashing | `argon2-cffi` + ~40-line in-repo wrapper with bcrypt-verify-then-rehash migration path. |
| Session signing | `itsdangerous.URLSafeTimedSerializer`. |
| Session timeout default | `"30d"`. Accepted values include `"never"`. |
| Password-change behaviour | Does **not** auto-revoke other sessions. |
| MFA / OIDC | Deferred. |
| Password reset | `GLADOS_AUTH_BYPASS=1` env var in compose. Disables auth entirely; admin visits the WebUI directly and changes passwords via Configuration → Users. Non-dismissable bright-red banner on every page while active. Flag can **only** be set in compose — no UI toggle. |

---

## 1. Executive summary

**Recommendation:** assemble auth from maintained primitives; ship with
a two-role model (`admin`, `chat`) plus an unauthenticated speech-service
tier. No framework, no sidecar, no policy engine.

Concretely:

- **Password hashing:** [`argon2-cffi`](https://pypi.org/project/argon2-cffi/)
  25.1.0 (Argon2id default) + ~40-line in-repo wrapper for bcrypt-legacy
  verify and rehash-on-login.
- **Session signing:** [`itsdangerous`](https://pypi.org/project/itsdangerous/)
  `URLSafeTimedSerializer` — the library Flask uses internally.
- **Users + roles:** `auth.users[]` list in `configs/global.yaml`;
  two fixed roles hard-coded in `glados/webui/permissions.py`; session
  + dynamic user state in `/app/data/auth.db` (SQLite).
- **Route tiers:**
  - **Public (unauth):** `/api/stt`, TTS endpoints (`/api/generate`,
    `/api/voices`, `/api/speakers`, `/api/attitudes`, `/files/`), the
    standalone TTS Generator at `/tts`, plus infrastructure routes
    (`/login`, `/setup`, `/logout`, `/health`, `/static/`, `/api/auth/`).
  - **Authed (any user):** Chat page, `/api/chat`, `/chat_audio/*`,
    `/chat_audio_stream/*`, main SPA shell at `/`.
  - **Authed admin:** everything else (Configuration, Memory, Logs,
    Audit, System, SSL, Users management).
- **First-run `/setup` wizard framework** creates the initial user,
  whose role is hard-coded to `admin` (no role field in the form —
  the first user is always the admin, by design). Subsequent users
  are created by that admin on the Users page with default role
  `chat`. The wizard is a pluggable step registry; Phase 1 ships one
  step (Set Admin Password) but the framework is designed so future
  steps (welcome / service-reachability check / HA token prompt /
  done screen per [roadmap.md § "Startup wizard UI"](roadmap.md))
  drop in without engine changes. See §5.1.
- **Password reset** via `GLADOS_AUTH_BYPASS=1` in compose — disables
  auth for the run, admin visits the WebUI directly and uses the
  normal Configuration → Users flow to change passwords. A
  non-dismissable bright-red banner is injected into every page while
  bypass is active. No YAML is mutated at boot; removing the env var
  and restarting restores normal auth. Flag is compose-only; no UI
  toggle.
- **MFA / OIDC / WebAuthn:** deferred.

This matches the operator's "surgical changes, reviewable chunks"
preference from [CLAUDE.md](../CLAUDE.md) §2 and the YAML-authoritative
pattern in [SELF_CONTAINMENT.md](SELF_CONTAINMENT.md) §"Architecture
decisions".

Sources are cited inline and collected in §14.

---

## 2. Current state (as of 2026-04-24, commit `e8cb13f`)

### 2.1 Storage

- One bcrypt hash in [`configs/global.yaml`](../configs/config.example.yaml) →
  `auth.password_hash` (string).
- One HMAC key in `configs/global.yaml` → `auth.session_secret` (64 hex chars).
- No session records on disk. Sessions are stateless signed cookies.
- No per-operator identity. All sessions carry `sub: "admin"`.

### 2.2 Model (glados/core/config_store.py:185-189)

```python
class AuthGlobal(BaseModel):
    enabled: bool = True
    password_hash: str = ""
    session_secret: str = ""
    session_timeout_hours: int = 24
```

### 2.3 Cookie format (glados/webui/tts_ui.py:480-532)

```
glados_session=<json_payload>.<hmac_sha256_hex>
```

`<json_payload>` carries `sub`, `iat`, `exp`, `jti`. Since 2026-04-20,
`exp` is always `0` (sentinel meaning "never expires"). Cookie flags
are `HttpOnly; SameSite=Strict; Secure (when HTTPS)`. `Max-Age` is
always `30 days` regardless of any `session_timeout_hours` value.

### 2.4 Rate limiting (glados/webui/tts_ui.py:535-557)

In-memory `dict[ip, (count, last_time)]`. 5 fails / 60 s. Lost on
restart. No audit emit on lockout.

### 2.5 Public routes on port 8052 today (glados/webui/tts_ui.py:461-477)

```
/login, /health
/api/generate, /api/chat, /api/stt, /api/files, /api/attitudes,
/api/speakers, /api/voices, /files/, /chat_audio/, /chat_audio_stream/,
/api/auth/, /static/
```

This rebuild changes `/api/chat` and `/chat_audio/*` from public to
"authed (any user)"; see §3.6 for the full gated/public split.

**Out of scope:** port 8015 (api_wrapper / Litestar) has its own
`/v1/audio/transcriptions` and `/api/chat` endpoints used by external
integrations. Those are a separate auth story; this document covers
**port 8052 only**.

### 2.6 First-run UX

Today operator runs:

```
docker exec -it glados python -m glados.tools.set_password
```

Then a container restart. The new design removes the shell-command
path in favor of `/setup` (§5.1) and the env-var reset flag (§9).

### 2.7 Live-reload gap (bug — important)

`tts_ui.py:444` claims *"Auth config (reloaded on each check to pick
up changes)"*. **Not what the code does.** Lines 445–448 read
`_cfg.auth.*` **once at module import** into module-level globals
`_AUTH_ENABLED`, `_AUTH_PASSWORD_HASH`, `_AUTH_SESSION_SECRET`,
`_AUTH_SESSION_TIMEOUT_H`. `_cfg.reload()` rebuilds the pydantic
models but nothing rebinds the module-level globals.

**Practical consequence:** password changes today require a container
restart to take effect, contrary to `MEMORY.md` →
`feedback_tools_and_timeouts` suggesting live-reload within ~30 s.

The new design explicitly fixes this by reading `_cfg.auth.*` live on
each call (§7.3).

### 2.8 Known file-level tech debt

[`docs/roadmap.md`](roadmap.md) § "Technical Debt" flags that
`glados/webui/tts_ui.py` has a UTF-8 BOM and mojibake. Auth-related
edits to this file should coordinate with that cleanup — not land
more mojibake. Comments currently contain `â€"` sequences (cp1252
em-dash saved as UTF-8); new comments should use actual `—`
characters.

---

## 3. Options evaluated

`http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler` is not
WSGI/ASGI, so framework-level auth packages can't be bolted on
without rewriting the handler. Five architectures were considered;
the RBAC layer was decided separately.

### 3.1 Architecture options

| Option | Description | Verdict |
|---|---|---|
| **A. Primitives in-process** (recommended) | `argon2-cffi` + `itsdangerous` + small SQLite session table. Keep stdlib handler. | ✅ Ship |
| **B. Rewrite to Litestar** | Rewrite `tts_ui.py` onto the framework `glados/api/app.py` already uses, pick up [Litestar session auth middleware](https://docs.litestar.dev/latest/reference/security/session_auth.html). | ❌ Too large — separate phase |
| **C. Rewrite to FastAPI + fastapi-users** | Full FastAPI migration + [`fastapi-users`](https://fastapi-users.github.io/fastapi-users/). | ❌ Larger than B; new framework we don't use elsewhere |
| **D. Sidecar forward-auth** | [Authelia](https://www.authelia.com/) as a second container, reverse-proxy forward-auth gates the WebUI port. | ❌ Violates self-containment |
| **E. Sidecar IdP** | [authentik](https://goauthentik.io/) as OIDC provider. | ❌ Requires PostgreSQL + Redis; violates self-containment |

### 3.2 Primitives choice

- `argon2-cffi` (Hynek Schlawack) — 25.1.0, explicit Python 3.13/3.14
  support ([PyPI](https://pypi.org/project/argon2-cffi/),
  [repo](https://github.com/hynek/argon2-cffi)).
- `itsdangerous` (Pallets, same project as Flask) — actively
  maintained ([PyPI](https://pypi.org/project/itsdangerous/),
  [Pallets docs](https://palletsprojects.com/p/itsdangerous/)).
- (Future, not Phase 1) `pyotp` for TOTP ([PyPI](https://pypi.org/project/pyotp/),
  [repo](https://github.com/pyauth/pyotp)).

We explicitly **do not** adopt:

- `passlib` — last release 2020; breaks on Python 3.13+; FastAPI
  docs have migrated away
  ([tracking issue](https://github.com/pypi/warehouse/issues/15454),
  [FastAPI discussion](https://github.com/fastapi/fastapi/discussions/11773)).
- `pwdlib` — v0.3.0 pre-1.0 with a single maintainer; operator
  preferred `argon2-cffi` + glue.
- `PyJWT` — overkill. `itsdangerous` has the same security properties
  and smaller surface.

### 3.3 RBAC layer

Operator has narrowed the requirement to two fixed roles and no custom
roles. A policy engine would be wildly overbuilt for this:

| Option | Verdict |
|---|---|
| **Two hard-coded roles + permission-string check** (recommended) | ✅ Ship. `admin` = all permissions, `chat` = `{chat.read, chat.send, webui.view.chat}`. ~60 LOC. |
| **Casbin / pycasbin** | ❌ Overkill for 2 roles / ~12 permissions ([PyPI](https://pypi.org/project/casbin/), [repo](https://github.com/casbin/pycasbin)). |
| **Oso open source** | ❌ **Deprecated** ([repo](https://github.com/osohq/oso)). |
| **Cadurso / OpenFGA** | ❌ Too young / too big. |

The two-role design follows the canonical
[Open-WebUI admin-vs-user pattern](https://deepwiki.com/open-webui/docs/5.3-roles-groups-and-permissions).
The permission-string abstraction (rather than `if role == "admin"` at
every call site) keeps the intent of each handler explicit and makes
Phase-2 role additions cheap — without needing them now.

### 3.4 Route gating map (operator-locked)

Port 8052 only. Port 8015 out of scope.

| Route | Tier | Required permission |
|---|---|---|
| `/login`, `/setup`, `/logout`, `/health` | Public (infra) | — |
| `/static/*`, `/api/auth/*` | Public (infra) | — |
| `/api/stt` | **Public** (STT service) | — |
| `/tts` (standalone TTS Generator page) | **Public** (TTS service) | — |
| `/api/generate`, `/api/voices`, `/api/speakers`, `/api/attitudes`, `/api/files`, `/files/*` | **Public** (TTS service) | — |
| `/` / SPA shell | Authed (chat or admin) | `webui.view` |
| `/api/chat`, `/chat_audio/*`, `/chat_audio_stream/*` | Authed (chat or admin) | `chat.send` |
| `/api/memory/*`, `/api/config/*`, `/api/logs/*`, `/api/audit/*`, `/api/system/*`, `/api/discover/*`, `/api/reload-engine`, `/api/ssl/*`, `/api/users/*` | **Admin only** | `admin` |

Unauthenticated requests to gated routes: 401 JSON for `/api/*`, 302
`/login` for HTML routes. Authenticated-but-unauthorized: 403 JSON for
`/api/*`, 403 HTML for HTML routes.

**TTS Generator as a standalone unauth page.** The current SPA at `/`
has TTS Generator as a tab of the main shell. Since the operator wants
TTS unauthenticated, we extract a lightweight `/tts` page: a plain
HTML form with text input and audio output, no sidebar, no auth. The
SPA shell at `/` drops the TTS Generator tab for `chat` users (admins
still see it as a convenience; they can also hit `/tts` directly).

**Service-endpoint rate limiting.** Because `/api/generate`, `/api/stt`,
and `/tts` are now reachable without auth, Phase 1 adds a per-IP token
bucket (`10 requests / 60 s`, configurable in `auth.rate_limits`) on
those paths. Mitigates incidental compute-cost abuse if the operator
ever exposes the WebUI beyond LAN. Not a replacement for a proper
reverse-proxy rate limiter; it's belt-and-braces defense in depth.

### 3.5 Reference designs

- Home Assistant — `hashlib.pbkdf2_hmac` SHA-512 100k iterations,
  storage in `.storage/auth_provider.homeassistant`
  ([HA auth docs](https://www.home-assistant.io/docs/authentication/),
  [developer docs](https://developers.home-assistant.io/docs/auth_api/)).
  Our scheme (Argon2id + SQLite sessions) is simpler and stronger.
- Immich — first user to register becomes admin via a web flow
  ([quick-start](https://docs.immich.app/overview/quick-start/)). Our
  `/setup` pattern.
- Open-WebUI — admin / user / pending model, optional first-admin env
  seeding; we use YAML instead of env seed
  ([roles and permissions](https://deepwiki.com/open-webui/docs/5.3-roles-groups-and-permissions),
  [discussion #5889](https://github.com/open-webui/open-webui/discussions/5889)).

---

## 4. Users, roles, and permissions

### 4.1 Shape

```
user  ─── has-one ──→  role (name)
role  ─── has-many ──→  permissions (string set)
route ─── requires ──→  permission
```

### 4.2 Roles (fixed)

| Role | Permissions | Intent |
|---|---|---|
| `admin` | `["*"]` (wildcard) | Full control. Created on first-run. Only admins can manage users. |
| `chat` | `{webui.view, chat.read, chat.send}` | Household members: talk to GLaDOS for home control via the chat tab. No other UI surface. |

Custom roles are **not** supported. Operators who need more granularity
can raise a scope expansion; the permission taxonomy below is the
design-side extension point.

### 4.3 Permission taxonomy (code constant)

Defined in a new `glados/webui/permissions.py`:

```python
PERMISSION_REGISTRY = frozenset({
    "webui.view",       # load SPA shell
    "chat.read",        # view chat history
    "chat.send",        # send a new chat turn
})

ROLES = {
    "admin": frozenset({"*"}),
    "chat":  frozenset({"webui.view", "chat.read", "chat.send"}),
}

def user_has_perm(role: str, perm: str) -> bool:
    perms = ROLES.get(role, frozenset())
    return "*" in perms or perm in perms
```

All admin-only routes use the sentinel permission `"admin"` rather
than inventing fine-grained permission strings for each surface. This
collapses the permission registry to a tiny set consistent with the
"only two roles, chat can do only chat" operator directive.

### 4.4 Route check helper

```python
def require_perm(handler, perm: str) -> bool:
    """
    Returns True if the request's session user satisfies `perm`.
    On failure, writes 401 (no session) or 403 (session but missing
    perm) and returns False. Handlers short-circuit on False, as
    today's _require_auth does.
    """
```

Every current `if not self._require_auth(): return` site is rewritten
to `if not require_perm(self, "<perm>"): return`. ~60 such sites
today; each is audited during implementation to pick the right
permission.

---

## 5. Architecture — data flows

### 5.1 First-run wizard framework

The first-run experience is a pluggable wizard. Phase 1 registers one
step (Set Admin Password). The engine is built so future phases can
drop in additional steps — welcome screen, upstream-service health
check, HA URL + token prompt, "you're done" summary — per the
existing [roadmap.md § "Startup wizard UI"](roadmap.md) without
touching the engine.

#### 5.1.1 Step abstraction

New module `glados/webui/setup/`:

```python
# glados/webui/setup/wizard.py

@dataclass(frozen=True)
class WizardStep:
    name: str              # URL-slug, e.g. "admin-password"
    title: str             # displayed as step header
    order: int             # sequence; lower runs earlier

    def is_required(self, cfg) -> bool:
        """True if this step still needs to run, given current state."""

    def render(self, handler) -> str:
        """Return HTML for the step's form, wrapped in the shared shell."""

    def process(self, handler, form) -> StepResult:
        """Validate, persist to YAML / DB, return 'done' or a next-step hint."""

# Registered steps (Phase 1):
STEPS: tuple[WizardStep, ...] = (
    SetAdminPasswordStep(order=100),
)

# Future (not in Phase 1 scope):
#   WelcomeStep(order=10)
#   HealthCheckStep(order=20)
#   SetAdminPasswordStep(order=100)    <-- already shipped
#   HaTokenStep(order=200)
#   DoneStep(order=999)
```

Routes:

| Route | Behaviour |
|---|---|
| `GET /setup` | Redirect (302) to the first `is_required()==True` step. If none remain, 302 `/`. |
| `GET /setup/<step_name>` | Render that step's HTML (guard-checks `is_required`; 302 forward if already satisfied). |
| `POST /setup/<step_name>` | Process input; on success, 302 to next required step or `/`. |

All `/setup/*` routes are public while `auth.bootstrap_allowed == true`.
Once the wizard completes (all required steps satisfied), the
framework flips `auth.bootstrap_allowed = false` and all `/setup/*`
routes 302 to `/login`.

#### 5.1.2 Flow (Phase 1, one step)

```
Fresh /app/configs volume
      │
      ▼
Operator visits https://<host>:8052/
      │
      ▼
GET /              → require_perm fails → 302 /login
GET /login         → sees auth.users == [] and bootstrap_allowed == true,
                     302 /setup
      │
      ▼
GET /setup         → wizard resolves first required step → 302 /setup/admin-password
      │
      ▼
GET /setup/admin-password
                   → render shared shell + step form:
                       - Username (free-text, case-sensitive, 1–64 chars, no control chars)
                       - Display name (optional, defaults to username)
                       - Password (min 8 chars, not in denylist)
                       - Confirm password
                     NOTE: no role field. The first user's role is hard-coded "admin".
      │
      ▼
POST /setup/admin-password
                   → validate inputs
                   → Argon2id-hash the password
                   → generate session_secret if missing
                   → merge-write global.yaml:
                       auth.users = [{
                         username, display_name, role: "admin",
                         password_hash, hash_algorithm: "argon2id",
                         disabled: false, created_at: <now>,
                       }]
                       auth.session_secret = <if newly generated>
                   → wizard engine checks remaining required steps
                       → none remain → flip auth.bootstrap_allowed = false
                   → create session row in auth.db, set cookie
                   → 302 /
      │
      ▼
Subsequent GET /setup or /setup/* → 302 /login (bootstrap_allowed is false)
```

#### 5.1.3 Role is hard-coded, not selected

The first-run form does **not** present a role dropdown. The first
user is always created with `role="admin"`. This matches the
operator's 2026-04-24 directive:

> "First run will always be an admin setting it up, so default role
> for first user should be admin."

Logic: the container on a fresh volume has no admin yet; the person
running `docker compose up -d` and reaching the wizard is by
definition the person setting up the instance. No amount of
dropdown-choosing would change that, and showing the dropdown only
invites a confused operator to pick `chat` and lock themselves into
a container with no admin. The role is set by the Phase 1 wizard
step and cannot be overridden from the form.

Adding additional users (including additional admins) is done later
via Configuration → Users (§5.6), where the dropdown *does* appear
and defaults to `chat`.

#### 5.1.4 Shared wizard shell

All wizard steps render inside a shared HTML shell that provides:

- GLaDOS branding header (matches login page style).
- Step indicator: "Step N of M: <step title>" with a row of dots.
- The step's form body (rendered by `step.render(handler)`).
- Optional "Back" link for multi-step futures (disabled when there
  is nothing to go back to).
- No sidebar, no tabs — the wizard replaces the normal SPA shell
  until complete.

The shared shell lives in `glados/webui/setup/shell.py` and is the
only template the wizard engine renders. Individual steps are pure
content.

#### 5.1.5 Extensibility (future phases, not shipped in Phase 1)

Adding a new step requires only:

1. Write a `WizardStep` subclass.
2. Add it to the `STEPS` tuple with an `order` value.
3. Ensure `is_required` returns `False` once the relevant config is
   set (so re-running `docker compose up -d` on a half-configured
   install resumes at the right step).

No engine changes, no routing changes, no session-cookie changes.
The wizard re-resolves the step list on every request.

### 5.2 Normal login

```
GET /login    → if authenticated, 302 /; else render login page with username+password form
POST /login   → check rate limit (per remote_addr + username pair) →
                look up user by username (exact, case-sensitive match).
                  404 returns the same "invalid credentials" as bad password to avoid enumeration.
                verify password:
                  if bcrypt-legacy hash (§6 migration): verify via bcrypt,
                  rehash via Argon2id, merge-write new hash for that user.
                create session row in auth.db (username, role_at_issue captured),
                sign cookie via itsdangerous,
                Set-Cookie, respond 200 JSON.
```

### 5.3 Every authenticated request

```
Request cookie "glados_session=<signed_token>"
      │
      ▼
itsdangerous.URLSafeTimedSerializer.loads(token, max_age=<effective_max_age>)
      │  payload shape: {"sid": "<uuid>", "u": "<username>", "iat": <int>}
      │
      ├─ BadSignature / expired      → 401 / 302 /login
      ▼
SELECT * FROM auth_sessions WHERE session_id=? AND revoked_at IS NULL
      ├─ no row                      → 401 / 302 /login
      ▼
Resolve user by username in cfg.auth.users → fetch current role
      ├─ user missing or disabled    → 401 / 302 /login
      ▼
Check required_permission for route  → 403 if role doesn't satisfy
      ▼
UPDATE auth_sessions SET last_used_at=now()
      ▼
Handler runs with request.user populated → audit log rows carry operator_id
```

### 5.4 Logout

```
GET /logout   → UPDATE auth_sessions SET revoked_at=now() WHERE session_id=?
              → Set-Cookie glados_session=; Max-Age=0
              → 302 /login
```

### 5.5 Password change (self)

```
POST /api/auth/change-password (authenticated)
  body: { current, new }
      │
      ▼
Verify `current` against the current user's password_hash.
      ▼
Validate `new` against length + denylist rules.
      ▼
Hash `new` with Argon2id, merge-write that user's password_hash.
      ▼
200 JSON { ok: true }
```

Other sessions of the same user are **not** revoked (operator
decision, 2026-04-24). Admin wanting to sign out other devices uses
the Active Sessions card (§7.4) to revoke individually.

### 5.6 Admin user management

```
GET  /api/users              → list all users (admin-only)
POST /api/users              → create user
                                body: {
                                  username,           // 1–64 chars, no control chars, case-sensitive
                                  display_name?,
                                  role,               // must be "chat" or "admin"
                                  password,           // min 8, not in denylist
                                }
PUT  /api/users/<u>          → update { role?, display_name?, disabled? }
POST /api/users/<u>/password → admin resets another user's password
DEL  /api/users/<u>          → delete user
                                400 if deleting the last admin
```

All require the admin sentinel permission. The **Add User** form in
the UI:

- `username` — text field, case-sensitive.
- `display_name` — optional text field.
- `role` — dropdown, options `chat` (default) and `admin`.
- `password` — text input with min-length + denylist validation.

Deleting/demoting the last admin returns 400 with explanatory error.
Prevents self-lockout.

---

## 6. Storage schema

### 6.1 `configs/global.yaml` — `auth:` block (new shape)

```yaml
auth:
  enabled: true
  session_secret: "<hex128>"          # itsdangerous signing key
  session_timeout: "30d"              # "never" | "<n>m|h|d|w" | <int seconds>
  session_idle_timeout: "0"           # "0" disabled | "<duration>"

  rate_limits:
    login_window_seconds: 60
    login_max_attempts: 5
    service_window_seconds: 60
    service_max_requests: 10          # applies to unauth /api/stt, /api/generate, /tts POST

  bootstrap_allowed: true             # flips false after first /setup completes

  users:
    - username: "ResidentA"            # case-sensitive, exact match at login
      display_name: "ResidentA"
      role: "admin"
      password_hash: "$argon2id$v=19$..."
      hash_algorithm: "argon2id"
      disabled: false
      created_at: 1713974400
    - username: "Sarah"
      display_name: "Sarah"
      role: "chat"
      password_hash: "$argon2id$v=19$..."
      hash_algorithm: "argon2id"
      disabled: false
      created_at: 1713974511

  # DEPRECATED fields retained for one release cycle for migration:
  password_hash: ""                   # Field(deprecated=True)
  session_timeout_hours: 0            # Field(deprecated=True); absorbed into session_timeout
```

**Written only via merge-write** — the partial-save bug fix at
`config_store.py:1143-1148` (2026-04-23) is preserved. Any single
action (add user, rotate password) mutates only the touched fields.

### 6.2 `/app/data/auth.db` — SQLite, new

Created automatically on first startup if missing, chmod 600 by the
container's user.

```sql
CREATE TABLE auth_sessions (
  session_id      TEXT PRIMARY KEY,     -- UUIDv4
  username        TEXT NOT NULL,        -- matches global.yaml auth.users[].username exactly
  role_at_issue   TEXT NOT NULL,        -- "admin" | "chat" captured at login; live role re-checked each request
  created_at      INTEGER NOT NULL,
  last_used_at    INTEGER NOT NULL,
  expires_at      INTEGER,              -- NULL = never
  revoked_at      INTEGER,              -- NULL = active
  user_agent      TEXT,
  remote_addr     TEXT,
  auth_method     TEXT NOT NULL DEFAULT 'password'
);
CREATE INDEX idx_auth_sessions_expires ON auth_sessions(expires_at) WHERE revoked_at IS NULL;
CREATE INDEX idx_auth_sessions_username ON auth_sessions(username) WHERE revoked_at IS NULL;

CREATE TABLE user_state (
  username              TEXT PRIMARY KEY,
  last_login_at         INTEGER,
  last_login_addr       TEXT,
  failed_login_count    INTEGER NOT NULL DEFAULT 0,
  last_failed_login_at  INTEGER
);

-- Deferred to Phase 2 (rate limits live in memory in Phase 1).
-- CREATE TABLE auth_rate_limits (...)
```

### 6.3 Why users in YAML but session state in SQLite?

- **Account records are authoritative** (YAML pattern): password hash,
  role, disabled flag. Operator inspects via `cat global.yaml`, edits
  via WebUI, restores from a single-file backup.
- **Session state is dynamic** (SQLite): last-login, failed-login
  counter, per-session revocation. Not appropriate to churn YAML on
  every login.

### 6.4 Secrecy expectations

| Item | Location | File mode | Secret? |
|---|---|---|---|
| `users[].password_hash` | global.yaml | 600 | Sensitive (Argon2id is slow but not impossible to crack). |
| `session_secret` | global.yaml | 600 | **Critical** — leak lets an attacker forge any cookie. Rotating invalidates every existing cookie. |
| Session rows | auth.db | 600 | Sensitive. Stolen file enables session replay until revocation. |
| `user_state` rows | auth.db | 600 | Not sensitive. |
| Rate-limit state | memory | n/a | Not sensitive. |

### 6.5 Password denylist (operator-approved)

Static list in `glados/webui/permissions.py`, ≤ 20 entries. Case-
insensitive match on submitted password.

```python
PASSWORD_DENYLIST = frozenset({
    "password", "passw0rd", "12345678", "123456789", "1234567890",
    "qwertyui", "qwerty12", "qwerty123", "letmein1", "abcd1234",
    "admin123", "password1", "glados12", "passpass", "11111111",
    "00000000", "welcome1", "baseball", "football", "monkey12",
})
```

Intentionally small — catches embarrassingly common passwords without
becoming a maintenance burden. Rejects return a generic "password too
weak" message to the UI. Can be extended as operator requests.

---

## 7. Session expiry policy

### 7.1 Defaults

| Field | Default | Rationale |
|---|---|---|
| `session_timeout` | `"30d"` | Matches current effective behaviour while giving the operator a knob. |
| `session_idle_timeout` | `"0"` (disabled) | Preserves the 2026-04-20 "no idle expiry" request. |
| `remember_me` checkbox | Removed from login | Current code accepts but ignores it. |

### 7.2 Accepted values for `session_timeout`

`"never"`, `"<n>m"`, `"<n>h"`, `"<n>d"`, `"<n>w"`, or a bare integer
(seconds). Parsed by a new ~20-LOC `glados/core/duration.py`.

### 7.3 Live-reload fix

Module-level `_AUTH_*` globals in `tts_ui.py` are **removed**. All
helpers read `_cfg.auth.<field>` directly on each request. Pydantic
attribute access is ~1 μs. Fixes §2.7.

### 7.4 WebUI surface changes

Admin-only (chat users never see any of these):

- **System → "Authentication & Audit" card** (existing, extended):
  session timeout select, idle timeout select, auth enabled toggle.
  `session_secret` stays advanced/hidden.
- **System → "Active Sessions" card** (new): table of sessions with
  Revoke buttons. Admin sees all users' sessions; chat users see only
  their own (if they ever reach the System page — which they won't,
  since the sidebar hides it).
- **Configuration → Users page** (new, admin-only): table of users,
  Add / Edit / Reset Password / Disable / Delete. Default role in
  Add form = `chat`; dropdown = `chat`, `admin`.

Any authenticated user:

- **Change Password** (accessible from profile menu): form with
  current / new / confirm. No "sign out other devices" checkbox.

Chat users effectively see: sidebar with only the Chat tab, a profile
menu with Change Password and Logout. Everything else is hidden at
render time (the page shell checks `currentUser.role` for each menu
entry) and enforced at the server (403 on direct access).

---

## 8. Rate limiting

Two separate limiters, both in-memory in Phase 1:

### 8.1 Login limiter — keyed by `(remote_addr, username)`

| Failures | Lockout |
|---|---|
| 1–2 | none |
| 3 | 2 s |
| 4 | 4 s |
| 5 | 8 s |
| 6+ | 60 s, no further escalation |

After `rate_limits.login_window_seconds` of no failures, counter
resets. Each lockout emits:

```
AuditEvent(kind="auth_lockout", origin=Origin.webui,
           username=<submitted>, remote_addr=<ip>, fail_count=<n>)
```

### 8.2 Service limiter — keyed by `remote_addr` on unauth TTS/STT

Applies to `/api/stt`, `/api/generate`, `/api/voices`, `/api/speakers`,
`/api/attitudes`, `/files/*`, `/tts` POST. Token bucket:
`rate_limits.service_max_requests` per `rate_limits.service_window_seconds`
(default 10 / 60 s). Exceeding returns 429 with `Retry-After` header.

This exists because these endpoints are now unauthenticated per operator
decision; it defends against incidental abuse without relying on an
external reverse-proxy rate limiter. Defaults are loose enough that a
single operator pushing the TTS Generator hard won't trip them.

Phase 2 adds SQLite persistence so restart doesn't reset the counters.

---

## 9. Emergency auth bypass via docker-compose

The operator asked for a compose-only recovery path that preserves all
container state. v4 simplifies the earlier "reset flag" idea: rather
than mutating YAML to clear password hashes at boot, the flag disables
the auth check entirely for the container's run. The admin visits the
WebUI directly, uses the normal Configuration → Users flow to reset
whatever passwords need resetting, then removes the flag and restarts.

### 9.1 Design

A single env var, readable only via compose:

| Env var | Effect |
|---|---|
| `GLADOS_AUTH_BYPASS=1` | Disables the auth middleware for the duration of the container's run. Every request is treated as an admin session. A non-dismissable bright-red banner is injected into every HTML page. No YAML or data is mutated at boot. |

**The flag is compose-only.** There is no WebUI toggle, no
`/api/*` endpoint to set it, no config key in `global.yaml`. The only
way in is `docker-compose.yml` → `environment:`. This is enforced by
reading the env var exactly once at container start and never rebinding
it from a config source.

### 9.2 Behaviour while bypass is active

- `require_perm(handler, any)` short-circuits to `True`.
- `/login`, `/setup`, `/logout` still exist (for reflex muscle memory)
  but the login form is replaced with a notice: *"Auth bypass is
  active. Go directly to https://&lt;host&gt;:8052/."*
- Session cookies are neither required nor created. If a cookie
  happens to be present, it is ignored.
- Every HTML response from the WebUI injects a fixed banner at the
  top of the document body, **before** any page content:
  ```
  ┌──────────────────────────────────────────────────────────────┐
  │  ⚠  AUTHENTICATION BYPASS MODE — anyone with network access │
  │     to this WebUI has full admin control. Remove             │
  │     GLADOS_AUTH_BYPASS from docker-compose.yml and restart   │
  │     the container to resume normal authentication.           │
  └──────────────────────────────────────────────────────────────┘
  ```
  Styling: full-width, `background: #c81010; color: #fff; padding: 12px;
  font-weight: 700; position: sticky; top: 0; z-index: 9999`. The
  banner cannot be dismissed from the UI — there is no close button,
  and the SPA shell's JavaScript does not have a hook to hide it.
- `/api/auth/status` reports `{"authenticated": true, "bypass": true,
  "user": {"username": "bypass", "role": "admin"}}` so the SPA shell
  renders the banner even on SPA-mediated navigation.
- Every audit event emitted during bypass is tagged
  `operator_id="bypass:<remote_addr>"` and carries `auth_bypass=true`.
  Future-you reading the audit log can see exactly what was done
  while the flag was on.
- A **loud** `logger.error()` is emitted at container start and every
  15 minutes thereafter while the flag is active. These land in
  `docker logs glados` and in the WebUI's Logs page.

### 9.3 What bypass does NOT do

- **Does not mutate `global.yaml`.** Existing `password_hash`,
  `session_secret`, user list, everything stays exactly as-is.
- **Does not clear sessions in `auth.db`.** When the operator removes
  the flag and restarts, any existing user sessions are still valid
  up to their expiry. (The operator should rotate `session_secret`
  separately if they suspect compromise.)
- **Does not delete or move anything.** Conversation DB, ChromaDB,
  audio files, TLS certs, logs, all untouched.
- **Does not affect port 8015** (api_wrapper / Litestar). Out of
  scope for this rebuild; bypass applies to port 8052 only.

### 9.4 Operator runbook

```yaml
# compose.yml — add the env line, then `docker compose up -d`
services:
  glados:
    environment:
      - TZ=${TZ:-UTC}
      - GLADOS_AUTH_BYPASS=1   # 1️⃣ add this ONLY when recovering
```

1. `docker compose up -d`
2. Browser → `https://<host>:8052` → WebUI loads directly, red banner
   at top. No login required.
3. Go to **Configuration → Users**.
4. For each account that needs a password change: click "Reset
   Password", type a new password, save. (For your own account:
   profile menu → Change Password. "Current password" field is
   skipped in bypass mode.)
5. Remove the `GLADOS_AUTH_BYPASS=1` line from `compose.yml`.
6. `docker compose up -d` again to restart without the flag.
7. Verify: the WebUI now redirects `/` to `/login`, banner gone.
8. Log in with the new password.

### 9.5 Safety notes

- **Physical/network access control is the only defense while bypass
  is on.** Ensure the WebUI port is not exposed to the public
  internet during the recovery window.
- **Don't edit config while bypass is on unless you need to.** Every
  action audits as `operator_id="bypass:<ip>"` rather than your
  normal identity, which makes later audit review noisier.
- **Remove the flag before other users log in.** Otherwise their
  cookies still work but they see the red banner on every page;
  they'll wonder why.
- **If you forget the flag is on** — the banner, the loud ERROR log,
  the periodic WARN, the audit trail, and the SPA's `bypass: true`
  status are all designed to make that as hard as possible.

### 9.6 Why this shape (vs. the v3 reset-by-YAML-mutation approach)

- **Surgical.** No boot-time writes. No chance of a partial mutation
  leaving YAML in a bad state.
- **Reversible.** Flip one env line, restart — you're back to where
  you started.
- **Leverages the UI you're already building.** Configuration → Users
  exists because the multi-user design needs it; bypass mode just
  removes the auth gate on the same flow.
- **Works for every reset scenario** with one flag: forgot admin
  password, forgot user password, accidentally deleted the last
  admin, accidentally disabled auth and locked yourself out.

`GLADOS_BOOTSTRAP=1` (from [roadmap.md § "Startup wizard UI"](roadmap.md))
is subsumed by this mechanism.

---

## 10. Migration from current bcrypt state

### 10.1 Rollout sequence

- **At startup,** `AuthGlobal` loads. If legacy-shape (`password_hash`
  set at top level, `users` absent), the loader synthesizes a single
  admin user in memory (YAML on disk not yet mutated):

  ```yaml
  auth:
    users:
      - username: "admin"
        display_name: "admin"
        role: "admin"
        password_hash: "$2b$12$..."      # legacy bcrypt
        hash_algorithm: "bcrypt-legacy"
        disabled: false
        created_at: <current ts>
    bootstrap_allowed: false            # admin already exists
  ```

- **On first successful login** against that synthesized admin:
  1. Verify via bcrypt succeeds.
  2. Rehash the submitted plaintext with Argon2id.
  3. Merge-write the new-shape YAML (with `users:` list and
     `hash_algorithm: "argon2id"`). Top-level `password_hash` /
     `hash_algorithm` cleared; `Field(deprecated=True)` markers retain
     the slots for one release cycle.
  4. Create session row as normal.
- **Existing cookies stay valid** for 30 days via a legacy-cookie
  fallback: if `itsdangerous.loads()` fails with `BadSignature`, try
  the legacy HMAC verify. On success, issue a fresh itsdangerous
  cookie + session row.

### 10.2 Deprecation timeline

- **Ship:** legacy-bcrypt verify + legacy-cookie fallback.
- **+ 30 days:** remove legacy-cookie fallback.
- **+ 90 days:** remove bcrypt-verify path. `hash_algorithm` accepts
  only `"argon2id"`.
- **+ 90 days:** delete `glados/tools/set_password.py`. All
  password-setting flows are now `/setup`, Users → Reset Password,
  `/api/auth/change-password`, or the env-var reset flag.

---

## 11. Rollback plan

Destructive changes:

1. New `auth:` YAML shape (users list, role, etc.).
2. New `/app/data/auth.db`.
3. New deps (`argon2-cffi`, `itsdangerous`).

Rollback:

1. Revert the code commit(s).
2. Before rollout, snapshot `global.yaml` →
   `global.yaml.pre-auth-rebuild`. On rollback, restore the top-level
   `password_hash` / `hash_algorithm` / `session_timeout_hours` from
   it. `users:` list is ignored by old code.
3. Restart.
4. `auth.db` becomes unreferenced (harmless leftover).
5. Login works with the restored bcrypt hash.

Documented in `docs/CHANGES.md` for the commits that land the rebuild.

---

## 12. What's deferred (out of Phase 1)

1. **MFA (TOTP, WebAuthn/passkeys).** Schema has `auth_method` column
   as the extension point. `pyotp` / `webauthn-python` or `fido2` as
   Phase-2 primitives.
2. **OIDC RP support.** `Authlib` 1.7.0 as the Phase-2 primitive
   ([PyPI](https://pypi.org/project/Authlib/)).
3. **Rate-limit persistence across container restarts.** Phase-2
   polish.
4. **More than two roles.** Operator has explicitly ruled out in Phase
   1; design leaves `ROLES` dict and `PERMISSION_REGISTRY` as the
   extension point.
5. **Password complexity beyond length + denylist.** NIST SP 800-63B
   recommends against composition rules, so no class requirements; no
   `Have I Been Pwned` API call.
6. **Signed-in-user indicator in the WebUI header.** Trivial to add
   once sessions carry user identity; Phase 1 ships a minimal profile
   menu with Change Password / Logout.
7. **Port-8015 auth.** Out of scope; tracked separately.

---

## 13. Implementation phases (preview — full plan comes after approval)

One commit per phase, self-contained and reversible.

| # | Change | ~LOC | Risk |
|---|---|---|---|
| 1 | Add `argon2-cffi`, `itsdangerous` to deps; new `glados/webui/permissions.py`; `AuthGlobal` schema changes with deprecated-field markers; live-reload fix (remove module-level `_AUTH_*`). | 250 | Low |
| 2 | `/app/data/auth.db` bootstrap + `glados/auth/sessions.py` session module. No behaviour change yet — legacy HMAC cookie still accepted. | 350 | Low |
| 3 | Migration synthesizer: on load, detect legacy shape and synthesize `users:[{admin}]` in memory. Login flow uses username + password. Existing tests updated. | 200 | Medium |
| 4 | `require_perm` helper; rewrite all `_require_auth` call sites. Route table from §3.4 enforced. | 400 | Medium — touches every handler |
| 5 | `glados/webui/setup/` wizard framework: `WizardStep` abstraction, step registry, shared shell, `/setup` + `/setup/<step>` routing, first-run detection. Phase 1 ships one step (`SetAdminPasswordStep`) with role hard-coded to `admin`; engine supports additional steps without changes. | 400 | Low |
| 6 | Extract standalone `/tts` page (unauth TTS Generator); remove TTS tab from SPA for `chat` users. | 200 | Low |
| 7 | `/api/users` CRUD + Configuration → Users WebUI page. | 500 | Medium |
| 8 | Active Sessions card + Change Password flow on System tab / profile menu. | 250 | Low |
| 9 | `GLADOS_AUTH_BYPASS` env-var plumbing: short-circuit `require_perm`, inject red banner on all HTML responses, tag audit events, loud startup + periodic WARN logs. | 180 | Low |
| 10 | Service-endpoint rate limiter on `/api/stt`, `/api/generate`, `/tts`. | 120 | Low |
| 11 | Remove legacy cookie acceptance; deprecate `set_password.py`; CHANGES.md entry. | 80 | Low |

Tests are written TDD-style per commit ([CLAUDE.md](../CLAUDE.md) §6).
Total estimate: ~2,700 LOC added, ~300 removed from `tts_ui.py`;
ten-to-twelve commits.

---

## 14. Sources

### Libraries

- `argon2-cffi` — [PyPI](https://pypi.org/project/argon2-cffi/),
  [repo](https://github.com/hynek/argon2-cffi),
  [docs](https://argon2-cffi.readthedocs.io/)
- `itsdangerous` — [PyPI](https://pypi.org/project/itsdangerous/),
  [Pallets site](https://palletsprojects.com/p/itsdangerous/)
- `passlib` status — [PyPI](https://pypi.org/project/passlib/),
  [PyPI warehouse issue #15454](https://github.com/pypi/warehouse/issues/15454),
  [FastAPI discussion #11773](https://github.com/fastapi/fastapi/discussions/11773)
- `pwdlib` — [PyPI](https://pypi.org/project/pwdlib/),
  [blog intro](https://www.fvoron.com/blog/introducing-pwdlib-a-modern-password-hash-helper-for-python/)
- `Authlib` — [PyPI](https://pypi.org/project/Authlib/),
  [repo](https://github.com/authlib/authlib)
- `pyotp` — [PyPI](https://pypi.org/project/pyotp/),
  [repo](https://github.com/pyauth/pyotp)
- `pycasbin` — [PyPI](https://pypi.org/project/casbin/),
  [repo](https://github.com/casbin/pycasbin),
  [Apache project](https://casbin.apache.org/)
- `oso` deprecated — [repo](https://github.com/osohq/oso),
  [migration issue](https://github.com/osohq/oso/issues/1742)

### Reference designs

- Home Assistant — [user docs](https://www.home-assistant.io/docs/authentication/),
  [providers](https://www.home-assistant.io/docs/authentication/providers/),
  [developer API](https://developers.home-assistant.io/docs/auth_api/)
- Immich first-run — [quick start](https://docs.immich.app/overview/quick-start/)
- Open-WebUI — [admin seeding discussion](https://github.com/open-webui/open-webui/discussions/5889),
  [roles and permissions](https://deepwiki.com/open-webui/docs/5.3-roles-groups-and-permissions)
- Authelia — [site](https://www.authelia.com/)
- Authelia vs authentik — [Cerbos 2026 comparison](https://www.cerbos.dev/blog/authelia-vs-authentik-2026-idp)
- Litestar session auth — [session_auth reference](https://docs.litestar.dev/latest/reference/security/session_auth.html)

### Internal repo

- [CLAUDE.md](../CLAUDE.md) — operator preferences
- [docs/SELF_CONTAINMENT.md](SELF_CONTAINMENT.md) — architectural direction
- [docs/roadmap.md § Authentication follow-ups](roadmap.md)
- [glados/webui/tts_ui.py](../glados/webui/tts_ui.py):439-746
- [glados/core/config_store.py](../glados/core/config_store.py):185-189
- [glados/tools/set_password.py](../glados/tools/set_password.py)

---

## 15. Change log

### v4 → v5 (2026-04-24, operator revision on first-run wizard)

- **First-run path formalized as a wizard framework** (§5.1).
  Pluggable `WizardStep` abstraction with a step registry, shared
  shell, and `/setup/<step_name>` routing. Phase 1 ships one step
  (Set Admin Password) but the engine supports additional steps
  without changes — matches the roadmap's deferred multi-step wizard
  (welcome / service health check / HA token prompt / done).
- **First user's role is hard-coded `admin`** (§5.1.3). No role
  dropdown on the first-run form. Matches the operator's rule:
  "First run will always be an admin setting it up, so default role
  for first user should be admin."
- **Phase 5 scope grown** (§13): 250 → 400 LOC to cover the wizard
  engine, shell, and step abstraction in addition to the single
  `SetAdminPasswordStep`.

### v3 → v4 (2026-04-24, operator revision on password reset)

- **Password-reset flag replaced with auth-bypass mode.** §9 rewritten.
  One env var `GLADOS_AUTH_BYPASS=1` instead of two; disables the auth
  check for the run rather than mutating YAML at boot. Admin uses the
  normal Configuration → Users flow to rotate passwords.
- **Non-dismissable red banner** injected into every HTML response
  while bypass is active (§9.2).
- **Flag is compose-only** — no UI toggle, no config-file entry, no
  API endpoint. Read once at container start.
- **No YAML or data mutation at boot**, which means bypass is fully
  reversible by removing the env line and restarting.
- **Audit events tagged** `operator_id="bypass:<ip>"` and
  `auth_bypass=true` so post-hoc review is unambiguous.
- **Phase 9 scope adjusted** (§13) to reflect the new mechanism; LOC
  estimate up from 120 to 180 because of banner injection + tagging.

### v2 → v3

- **Roles collapsed from three to two.** `viewer` removed; `admin`
  and `chat` only, no custom roles (operator decision 2026-04-24).
- **`chat` role narrowed** to the minimum: `webui.view`, `chat.read`,
  `chat.send`. No memory, no logs, no TTS Generator, no audit view.
  Matches the operator's directive: "Chat has no settings options,
  only chat for home control."
- **TTS endpoints now unauthenticated** (operator added to the
  public-tier list). `/api/generate`, `/api/voices`, `/api/speakers`,
  `/api/attitudes`, `/api/files`, `/files/*` all move out of the
  gated tier. Standalone `/tts` page extracted as an unauthed entry
  point (§3.4).
- **Service-endpoint rate limiter** added (§8.2) because TTS + STT
  are unauthenticated. 10 requests / 60 s per IP by default. Defense
  in depth for a LAN-only WebUI; doesn't substitute for reverse-proxy
  limits if exposed.
- **Password denylist** added (§6.5) — 20 common weak passwords,
  per operator request.
- **Username rules locked** (§5.1, §5.6): case-sensitive exact match
  to whatever the admin typed at add-user time. No canonicalization.
  Validation: non-empty, ≤ 64 chars, no control characters.
- **Permission taxonomy simplified** (§4.3): just `webui.view`,
  `chat.read`, `chat.send`, plus the `admin` wildcard sentinel.
  Admin-only routes use the `"admin"` sentinel rather than individual
  fine-grained strings.
- **§13 phase preview** extended to eleven phases (added standalone
  `/tts` extraction and service-endpoint rate limiter).

---

## 16. What happens next

This document is **draft v3**. On approval I will:

1. Commit and push this document as-is.
2. Open a companion implementation plan (`docs/AUTH_PLAN.md`)
   breaking the rebuild into the phases in §13 with per-phase test
   plans. No code lands until the plan is also reviewed.

No open questions remain from the operator's side. One implementation
note worth flagging now so it doesn't surprise us later:

- **Phase 6 UX detail — the standalone `/tts` page.** Plain HTML
  form: textarea for input, voice/speaker dropdowns populated from
  `/api/voices` + `/api/speakers`, `Submit` → audio tag. No sidebar,
  no auth, no session cookie consumed. Mirrors the compact
  speech-service tools exposed by many self-hosted AI stacks. The
  admin still has full TTS Generator inside the SPA shell.
