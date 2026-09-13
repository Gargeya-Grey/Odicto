# OpenAI subscription (ChatGPT Plus/Pro) login for Odicto AI mode — feasibility research

**Research date:** 2026-09 (all sources read directly; links resolve as of that date)
**Question:** Can Odicto's AI mode authenticate with the user's *ChatGPT subscription* (OAuth) instead of an API key, so that subscribers use their plan's own usage limits?
**Deliverable status:** research note only. No code in this repo was changed.

---

## Bottom line / verdict

**Technically possible today. Not officially documented, not officially permitted, and explicitly not offered by OpenAI as a program for third-party apps.** This is category **(B) — technically possible but not documented / not permitted**, with a thin layer of **(A)** around exactly one thing: OpenAI's own first-party clients (Codex CLI, ChatGPT desktop app, Codex IDE extension) sign in with a ChatGPT subscription by design, and Sam Altman publicly blessed *one* third-party tool for it in May 2026 (see [Precedent](#precedent-and-enforcement)).

The mechanism is real and fully observable, because OpenAI's Codex CLI is open source (Apache-2.0) and the OAuth flow is in plain sight in `codex-rs/login/`. Concretely: issuer `https://auth.openai.com`, authorization-code + PKCE (S256) with a hardcoded public `client_id` `app_EMoamEEZ73f0CkXaXp7hrann`, loopback redirect `http://localhost:1455/auth/callback`, scopes `openid profile email offline_access …`, refresh via `POST https://auth.openai.com/oauth/token`, tokens stored in `~/.codex/auth.json`. The resulting bearer token is **not** valid against `api.openai.com`; it is valid only against the ChatGPT/Codex backend (`https://chatgpt.com/backend-api/codex/…`), which is a Responses-shaped, Codex-flavoured endpoint that a third party has demonstrated receiving a plain one-shot call from ([openai/codex#36886](https://github.com/openai/codex/issues/36886)).

**Verdict for Odicto: do not ship this as a supported AI-mode provider.** The 401/403 failure modes are unpredictable, there is no published contract (OpenAI has been asked directly and has not answered — [#36886](https://github.com/openai/codex/issues/36886) is open with zero comments; [#24971](https://github.com/openai/codex/issues/24971) was closed with zero comments), and the ChatGPT Terms of Use reserve the right to suspend accounts for circumventing protective measures. The honest, low-risk paths are: keep the API-key providers as-is, and **optionally document the official Codex CLI as a user-side external tool** for subscribers who want it, clearly marked as unsupported by Odicto.

---

## What is actually possible

### 1. Officially: ChatGPT sign-in exists — for OpenAI's own clients only

OpenAI documents two sign-in methods for Codex, and both are scoped to OpenAI's own surfaces:

> "Codex supports two ways for a person to sign in when using OpenAI models: Sign in with ChatGPT for subscription access; Sign in with an API key for usage-based access. The ChatGPT desktop app, Codex CLI, and IDE extension support both sign-in methods for local work. Codex cloud requires signing in with ChatGPT."
> — [Authentication | Codex docs](https://developers.openai.com/codex/auth) (redirects to `learn.chatgpt.com/docs/auth`)

> "Run `codex` and select **Sign in with ChatGPT**. We recommend signing into your ChatGPT account to use Codex as part of your Plus, Pro, Business, Edu, or Enterprise plan."
> — [openai/codex README](https://github.com/openai/codex/blob/main/README.md)

> "Codex is included across ChatGPT plans, including Free and Go. Usage limits vary by plan."
> — [Using Codex with your ChatGPT plan (help.openai.com #11369540)](https://help.openai.com/en/articles/11369540-codex-in-chatgpt)

**There is no "Sign in with ChatGPT for your app" program.** No client registration page, no published OAuth scopes, no developer docs for third-party ChatGPT-subscription auth. Requests for it on OpenAI's own developer community were closed without an answer:
[thread 1378506, 2026-04-04](https://community.openai.com/t/login-with-chatgpt-allow-users-to-use-their-own-plus-subscription-in-3rd-party-apps/1378506) → auto-closed 24 h later with 0 replies.

### 2. The actual mechanism (from Codex CLI source — publicly readable, but not a published contract)

Everything below is read directly from `openai/codex@main`. The files are Apache-2.0 and public; reading them is not reverse engineering, but the endpoints are **not** documented by OpenAI as a third-party contract.

| Item | Value | Source |
|---|---|---|
| Issuer / OAuth base | `https://auth.openai.com` (`DEFAULT_ISSUER`) | [`codex-rs/login/src/server.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/server.rs) |
| Authorize endpoint | `{issuer}/oauth/authorize` | same |
| Query params | `response_type=code`, `client_id`, `redirect_uri`, `scope`, `code_challenge`, `code_challenge_method=S256`, `id_token_add_organizations=true`, `codex_cli_simplified_flow=true`, `state`, `originator` | same |
| Scopes | `openid profile email offline_access api.connectors.read api.connectors.invoke` | same |
| PKCE | verifier = base64url(64 random bytes), challenge = base64url(SHA-256(verifier)), method `S256` | [`codex-rs/login/src/pkce.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/pkce.rs) |
| Public client_id | `app_EMoamEEZ73f0CkXaXp7hrann` (`pub const CLIENT_ID`), overridable via env `CODEX_APP_SERVER_LOGIN_CLIENT_ID` | [`codex-rs/login/src/auth/manager.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/auth/manager.rs) |
| Redirect URI | `http://localhost:{actual_port}/auth/callback`; default port **1455**, fallback **1457** — comment: *"Keep in sync with the Codex CLI Hydra redirect URI allow-list."* | [`server.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/server.rs) |
| Token endpoint (code exchange, form-encoded) | `POST https://auth.openai.com/oauth/token` with `grant_type=authorization_code&code=…&redirect_uri=…&client_id=…&code_verifier=…` | same |
| Token refresh (JSON) | `POST https://auth.openai.com/oauth/token` body `{client_id, grant_type:"refresh_token", refresh_token}` | [`auth/manager.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/auth/manager.rs) |
| Revoke endpoint | `https://auth.openai.com/oauth/revoke` (env override `CODEX_REVOKE_TOKEN_URL_OVERRIDE`) | same |
| Token storage | `$CODEX_HOME/auth.json` (default `~/.codex/auth.json`); modes `file` / `keyring` / `auto` / `ephemeral` | [`auth/storage.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/auth/storage.rs), [auth docs](https://developers.openai.com/codex/auth) |
| `auth.json` shape | `{ auth_mode, OPENAI_API_KEY?, tokens: { id_token, access_token, refresh_token, account_id? }, last_refresh, agent_identity?, personal_access_token?, bedrock_*? }` | [`auth/storage.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/auth/storage.rs) |
| Refresh behaviour | Proactive refresh; refresh window 5 min before expiry; `TOKEN_REFRESH_INTERVAL = 8` (days). Error codes distinguished: `refresh_token_expired`, `refresh_token_reused`, `refresh_token_invalidated` | [`auth/manager.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/auth/manager.rs) |
| Device-code flow | Exists: `codex login --device-auth` ("beta"), requires the user to enable device code login in ChatGPT security settings or a workspace admin to allow it | [auth docs](https://developers.openai.com/codex/auth) |

**The `chatgpt_account_id` claim.** The ID token is parsed for `https://api.openai.com/auth` claims:

```rust
struct AuthClaims {
    chatgpt_plan_type: Option<PlanType>,   // "free" | "plus" | "pro" | "business" | "enterprise" | "edu" | …
    chatgpt_user_id: Option<String>,
    chatgpt_account_id: Option<String>,    // the workspace/account id
    chatgpt_account_is_fedramp: bool,
}
```
— [`codex-rs/login/src/token_data.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/token_data.rs)

`chatgpt_account_id` is persisted as `TokenData.account_id` and ends up as the request-scoped account identifier. The app-server protocol exposes it as `previousAccountId` in `ChatgptAuthTokensRefreshParams` with the note *"Clients that manage multiple accounts/workspaces can use this as a hint to refresh the token for the correct workspace. This may be `null` when the prior auth state did not include a workspace identifier (`chatgpt_account_id`)."* — [`app-server-protocol/schema/json/ChatgptAuthTokensRefreshParams.json`](https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/schema/json/ChatgptAuthTokensRefreshParams.json)

**A second, lesser-known grant: subscription token → API-key-shaped token.** During login, Codex exchanges the ID token for an API-key-shaped access token:

```
grant_type=urn:ietf:params:oauth:grant-type:token-exchange
client_id=<CLIENT_ID>
requested_token=openai-api-key
subject_token=<id_token>
subject_token_type=urn:ietf:params:oauth:token-type:id_token
```
— `obtain_api_key()` in [`server.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/server.rs). The result is stored as `OPENAI_API_KEY` alongside the ChatGPT tokens. Its practical validity outside Codex is **unverified** here.

### 3. Which backend the subscription token is valid against

The subscription bearer is **not** an `api.openai.com` credential. OpenAI's official `curl`/SDK path requires a platform API key ([API keys dashboard](https://platform.openai.com/api-keys), [auth docs](https://developers.openai.com/codex/auth): *"If you sign in with an API key, Codex uses standard API pricing instead of included ChatGPT plan credits"*). The subscription token targets the ChatGPT/Codex backend:

- `https://chatgpt.com/backend-api/codex` is the Production base URL (asserted in `codex-rs/agent-identity/src/lib.rs` tests: `ChatGptEnvironment::from_chatgpt_base_url("https://chatgpt.com/backend-api/codex")? == ChatGptEnvironment::Production`).
- The path is configurable and admin-pinnable as `chatgpt_base_url` ([auth docs](https://developers.openai.com/codex/auth): *"Admins can enforce `cli_auth_credentials_store` and `chatgpt_base_url`"*).

**Headers.** From source, the default client sets:

```rust
pub const DEFAULT_ORIGINATOR: &str = "codex_cli_rs";
pub const RESIDENCY_HEADER_NAME: &str = "x-openai-internal-codex-residency";
// default_headers(): "originator: <value>", "User-Agent: codex_cli_rs/<version> (…)…", optional residency header
```
— [`codex-rs/login/src/auth/default_client.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/auth/default_client.rs)

`get_account_id()` also reads a `chatgpt-account-id` header off externally-supplied header auth ([`auth/manager.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/auth/manager.rs)).

**(B) Observed-but-not-in-source header set.** A third-party developer documented the working request shape against that endpoint — `ChatGPT-Account-ID`, `OpenAI-Beta: responses=v1`, `originator`, `version`, `session_id` — and reported it *"works today… by observation, not by contract"* ([#36886](https://github.com/openai/codex/issues/36886)). The same flow is documented independently by OpenClaw ([openclaw/docs/concepts/oauth.md](https://github.com/openclaw/openclaw/blob/main/docs/concepts/oauth.md)). Treat these as **reverse-engineered, unstable**.

### 4. Is the endpoint API-compatible, or Codex coupled?

**Partly compatible, but coupled.** Evidence:

- It is **Responses-shaped**, not Chat Completions. The same third party ran a plain, non-agentic request and got correct structured output: *"One request, `gpt-5.4-mini`, a forced tool call, 21,212 input / 84 output tokens, ~30s — correct structured output"* ([#36886](https://github.com/openai/codex/issues/36886)). So a simple text-in/text-out refiner call is **not** architecturally impossible.
- But it is gated by client provenance (`originator`, `version`, `session_id`), and the surface moves: [#33969](https://github.com/openai/codex/issues/33969) reports `403 {"error":"invalid or disabled credential"}` from a Codex relay after a client version bump, with the reporter's own diagnosis that *"the relay endpoint appears to reject ChatGPT OAuth tokens — it recognizes the token format (returns 403, not 401) but marks the credential as 'invalid or disabled'"*. [#29243](https://github.com/openai/codex/issues/29243) is cited for inconsistent plan-type reporting.
- Also **streaming/event-protocol coupled** in practice: Codex's own client consumes a streamed event protocol (`ResponseStream`, `codex_api::ResponseEvent` — [`core/src/client_common.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/client_common.rs)). A non-streaming `stream:false` call works (as demonstrated), but nothing guarantees it.

### 5. Which models are reachable with a subscription

**Not arbitrary model names.** Model availability under ChatGPT sign-in is a curated, plan-and-surface-gated list, and it is *different* from platform API availability:

> "Availability depends on the rollout, your sign-in method, and your client."
> "`gpt-5.4` and `gpt-5.4-mini` retire from Codex with ChatGPT sign-in on August 31, 2026. … The OpenAI API and Codex authenticated with your own API key aren't affected."
> — [Codex Models docs](https://developers.openai.com/codex/models)

The models table on that page carries per-model flags; e.g. `gpt-5.3-codex-spark` has **`"API Access": false`** while Astra/Sol/Terra/Luna have `"API Access": true`. Codex itself hardcodes preferred model IDs (e.g. `gpt-5.6-luna` / `gpt-5.6-terra` for memory jobs — see [#39272](https://github.com/openai/codex/issues/39272)). So an Odicto "ChatGPT subscription" provider would inherit a model list that changes under it, without notice, and that is a *subset* of what an API key could use.

---

## What OpenAI's terms say

### ChatGPT consumer terms — [Terms of Use (rest of world, effective 2026-01-01)](https://openai.com/policies/row-terms-of-use/)

Relevant clauses (quoted verbatim):

> **Registration.** "You must provide accurate and complete information to register for an account to use our Services. **You may not share your account credentials or make your account available to anyone else** and are responsible for all activities that occur under your account."

> **What you cannot do.** "…you may not:
> • Modify, copy, lease, sell or distribute any of our Services.
> • Attempt to or assist anyone to reverse engineer, decompile or discover the source code or underlying components of our Services…
> • Automatically or programmatically extract data or Output…
> • **Interfere with or disrupt our Services, including circumvent any rate limits or restrictions or bypass any protective measures or safety mitigations we put on our Services.**"

> **Termination.** "We reserve the right to suspend or terminate your access to our Services or delete your account if we determine: • You breached these Terms or our Usage Policies. • We must do so to comply with the law. • Your use of our Services could cause risk or harm to OpenAI, our users, or anyone else."

The **EU/EEA/UK/CH variant** ([Europe Terms of Use, updated 2026-01-16](https://openai.com/policies/terms-of-use/)) says the same plus a broader disclosure/authorisation right:

> "**OpenAI rights**. We may take action to restrict, suspend, or terminate your access to our Services or close your account if we determine, acting reasonably and objectively: • You breached these Terms… • Your use of our Services could cause risk or harm to OpenAI, our users, or anyone else."

### Usage policies — [openai.com/policies/usage-policies](https://openai.com/policies/usage-policies/) (effective 2025-10-29)

> "We hold people accountable for inappropriate use of our services, and **breaking or circumventing our rules and safeguards may mean you lose access to our systems or experience other penalties.**"

> Prohibited list includes: "…**circumventing our safeguards**…"

### Business/API side — [OpenAI Services Agreement](https://openai.com/policies/services-agreement/) (effective 2026-01-01)

This governs the API, ChatGPT Enterprise/Business/Edu (per its own scope note, and per the Codex help article: *"When you sign in to Codex using an existing ChatGPT account, the ChatGPT Terms of Use and Privacy Policy—or the corresponding online services agreement for OpenAI API and ChatGPT Enterprise, Education or Business Users—apply"*). §3.3 Restrictions prohibit:

> "(g) buy, sell, or transfer API keys from, to, or with a third party; (h) interfere with or disrupt the Services, including circumvent any rate limits or restrictions or bypass any protective measures or safety mitigations for the Services; (i) violate or circumvent Usage Limits or otherwise configure the Services to avoid Usage Limits."

### What the terms do **not** say

**No clause in any of the above names third-party clients, unofficial clients, or ChatGPT-subscription OAuth reuse.** There is no published statement that "ChatGPT subscription credentials may only be used with OpenAI's own clients." That absence is the crux of the problem — it is why the [openai/codex#36886](https://github.com/openai/codex/issues/36886) author asks for exactly one of: a documented header contract, *or* an explicit "no", *or* a stability note, and gets none.

The closest thing to a stated boundary is the enterprise access-token feature, which is explicitly scoped:

> "Use an access token when automation needs ChatGPT workspace access, ChatGPT-managed Codex entitlements, or enterprise workspace controls **without a browser sign-in**. Access tokens are intended for trusted scripts, schedulers, and private CI runners. **For general OpenAI API calls, continue to use Platform API keys.**"
> — [auth docs](https://developers.openai.com/codex/auth)

and:

> "Use API key authentication for programmatic Codex CLI workflows, such as CI/CD jobs. Don't expose Codex execution in untrusted or public environments."
> — same page

Read plainly: OpenAI's published position is that **automation should use an API key**, and the only sanctioned non-interactive *subscription* credential is the Enterprise-granted `CODEX_ACCESS_TOKEN` piped through `codex login --with-access-token`.

---

## Precedent and enforcement

### Third-party tools that ship "use your ChatGPT subscription" login

**OpenCode — first-class, documented, shipped.** OpenCode's own provider docs list OpenAI with a ChatGPT option:

> "We recommend signing up for [ChatGPT Plus or Pro](https://chatgpt.com/pricing). 1. Run the `/connect` command and select OpenAI. 2. Here you can select the **ChatGPT Plus/Pro** option and it'll open your browser and ask you to authenticate. 3. Now all the OpenAI models should be available…"
> "**Compute residency:** For ChatGPT OAuth, OpenCode automatically applies a regional inference residency requirement when one is advertised by your workspace credentials. It forwards the compute residency value from the credential…"
> — [OpenCode Providers docs](https://opencode.ai/docs/providers/)

The same page draws the line against Anthropic explicitly — and, in doing so, implicitly characterises the ChatGPT case as permitted:

> "There are plugins that allow you to use your Claude Pro/Max models with OpenCode. **Anthropic explicitly prohibits this.** Previous versions of OpenCode came bundled with these plugins but that is no longer the case as of 1.3.0. Other companies support freedom of choice with developer tooling - **you can use the following subscriptions in OpenCode with zero setup: ChatGPT Plus, Github Copilot, Gitlab Duo**"

**OpenClaw — claims explicit permission, and a CEO tweet backs it.** OpenClaw's own docs:

> "OpenAI Codex OAuth is explicitly supported for use **outside the Codex CLI**, including OpenClaw workflows."
> Flow: "…open `https://auth.openai.com/oauth/authorize?...` (scope `openid profile email offline_access`); try to capture the callback on `http://localhost:1455/auth/callback` (the callback host defaults to `localhost` and **only accepts loopback hosts**); … exchange the code at `https://auth.openai.com/oauth/token`; extract `accountId` from the access token and store `{ access, refresh, expires, accountId }`."
> — [openclaw/docs/concepts/oauth.md](https://github.com/openclaw/openclaw/blob/main/docs/concepts/oauth.md)

This independently corroborates the codex-source mechanism (same issuer, same loopback callback shape, same scope set, same accountId extraction) and adds a useful operational warning: refresh tokens rotate, so *"log in via OpenClaw and via … Codex CLI, and one of them randomly gets logged out later"* — their store is designed as a "token sink".

**OpenAI's CEO blessed it, publicly, at least once:**

> "@sama — you can sign in to openclaw with your chatgpt account now and use your subscription there! happy lobstering." — Sam Altman, 2026-05-01
> — [x.com/sama/status/2050357911915028689](https://x.com/sama/status/2050357911915028689)

This is a **public statement by OpenAI's CEO that a named third-party tool may use ChatGPT subscription auth**. It is significant — but it is a tweet about one tool, not a policy, not a developer program, and it does not generalise to "any app may do this". No follow-up documentation has been published (see next).

**Other ecosystem entries (lower confidence — project-level claims, not OpenAI statements):** community plugins that wrap the same flow exist for OpenCode, e.g. [`numman-ali/opencode-openai-codex-auth`](https://github.com/numman-ali/opencode-openai-codex-auth) (its own notice: *"This plugin is for personal development use only."*). Cline, Continue, LibreChat and Aider were **not** found shipping ChatGPT-subscription OAuth; do not assume they do.

### What OpenAI actually said, on its own channels

**P1 — Staff/moderator on OpenAI's Developer Community (2026-05-08), on the exact question:**

> "There are no official docs according to my best knowledge. This is how Openclaw does it: [link]"
> "…In case it helps, Opencode appears to have native Codex/ChatGPT subscription OAuth, not only a third-party plugin. But that's also something to improve the docs. **I'll pass it along.**"
> — [community.openai.com/t/…/1380525](https://community.openai.com/t/login-with-chatgpt-instead-of-bring-your-own-token/1380525) (poster is flagged `moderator: true, staff: true`; the thread was closed 2026-05-27 with no further answer)

**P2 — The canonical ask, unanswered.** [openai/codex#36886](https://github.com/openai/codex/issues/36886) *"Is there a documented auth contract for third-party clients using a ChatGPT subscription with the Responses API?"* — state **open**, `comments: 0`, labelled `documentation`/`auth`/`CLI`. Its requested outcomes include: *"An explicit 'no' — subscription credentials are for first-party clients; third parties should use a platform API key. That's a completely reasonable answer and I'd implement to it immediately. Right now the absence of a statement is the problem, not the answer itself."*

**P3 — Same question for mobile, closed with no answer.** [openai/codex#24971](https://github.com/openai/codex/issues/24971) *"What is the supported Codex auth flow for native mobile clients?"* — closed 2026-05-28, `comments: 0`, locked.

**P4 — No enforcement evidence found.** I found **no primary source** (OpenAI policy page, help article, official repo comment) documenting account suspension, ban, or rate-limit clamping specifically for reusing ChatGPT-subscription OAuth in a third-party app. OpenCode and OpenClaw both ship it and neither documents a strike against them. This should be read as *"not documented"*, not as *"permitted"* — the ToS explicitly reserves the suspension right and the Usage Policies warn that circumvention "may mean you lose access to your systems".

**P5 — The surface visibly shifts.** Evidence that OpenAI actively gates and re-gates this endpoint (whether deliberately or as collateral):
- [#33969](https://github.com/openai/codex/issues/33969) — `403 "invalid or disabled credential"` after a client version bump; reporter notes the credential is *recognised* but marked disabled.
- [#29243](https://github.com/openai/codex/issues/29243) — plan type reported inconsistently (cited by #36886).
- [#40564](https://github.com/openai/codex/issues/40564) — user on a "third-party token plan" complaining about background model consumption; labelled `rate-limits`.
- The entitlement gate is real and named in source: a missing Codex entitlement surfaces as `access_denied` + `missing_codex_entitlement` → *"Codex is not enabled for your workspace"* ([`server.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/server.rs)). **A ChatGPT Free/Business/Edu workspace without Codex entitlement cannot use this at all.**

### Consequences summary

| Outcome | Status |
|---|---|
| OpenAI published a third-party ChatGPT-OAuth program | **No** ([auth docs](https://developers.openai.com/codex/auth) scope; [#36886](https://github.com/openai/codex/issues/36886) unanswered) |
| OpenAI publicly blessed one third-party tool | **Yes, once** ([@sama, 2026-05-01](https://x.com/sama/status/2050357911915028689)) |
| OpenAI ToS clause naming third-party clients | **No** |
| Documented account bans for this specific reuse | **None found** |
| Tools shipping it anyway | **Yes** — [OpenCode](https://opencode.ai/docs/providers/) (first-class), [OpenClaw](https://github.com/openclaw/openclaw/blob/main/docs/concepts/oauth.md) |
| Endpoint stable | **No** — credential gating tied to client identity/version ([#33969](https://github.com/openai/codex/issues/33969)) |

---

## Fit with Odicto's architecture

Grounded in `config.py`, `refiner.py`, `setup_web.py` as they exist today.

### What the provider layer looks like

`config.py` treats a provider as **a closed enum plus a static key plus a static base URL**:

- `ENV_DEFAULTS["LLM_PROVIDER"] = "none"` and `LLM_PROVIDER: Literal["ollama","openrouter","meta","gemini","none"]` (`config.py:53`, `config.py:397-403`).
- `Config.validate()` hard-fails on anything else: `valid_providers = {"ollama","openrouter","meta","gemini","none"}` (`config.py:943-947`).
- `effective_api_key()` returns a **plain string**, per provider, from a static env var (`config.py:523-540`). There is no notion of an expiring credential, a refresh, or a 401-recovery retry.
- `effective_llm_api_base()` returns a **static base URL** per provider, chosen by `if/elif` (`config.py:506-521`).
- `effective_llm_model()` cascades `*_MODEL` → `LLM_MODEL` → default (`config.py:465-504`).
- `config_warnings()` warns loudly about any `.env` key not in `ENV_DEFAULTS` (`config.py:1096-1135`) — so new keys must be registered or every user sees "Unknown .env key".

`refiner.py` builds one client per provider at construction time:

- `ollama` / `openrouter`: `OpenAI(base_url=…, api_key=…)`; OpenRouter also injects `default_headers={"HTTP-Referer":…, "X-Title":"Odicto"}` (`refiner.py:358-386`) — **the only existing example of custom request headers**, and exactly the hook a ChatGPT provider would need (`originator`, `ChatGPT-Account-Id`, `session_id`).
- `meta`: hand-rolled `_MetaClient` using a `requests.Session` with `Authorization: Bearer <key>` fixed at construction, posting to `{base}/responses` (`refiner.py:132-243`). **This is the closest architectural analogue**: a small custom client class, per-provider, no SDK.
- `gemini`: official `google-genai` SDK, `client.interactions.create(...)` (`refiner.py:263-343`).
- Failure policy: any exception → log and **return the raw transcript** (`refiner.py:681-696`). "Dictation never fails" is a hard design invariant.

`setup_web.py` hard-codes the provider set **in six places**:
1. `EDITABLE_KEYS` (`setup_web.py:41-74`) — and the merge writer requires every editable key to have a positioned line in `.env.example`, enforced by tests.
2. `_mask_key()` (`setup_web.py:85-90`) and `_SECRET_KEYS` (`setup_web.py:101`).
3. `validate_provider_requirements()` (`setup_web.py:112-139`).
4. The provider `<select>` menu + per-provider key fields in the HTML (`setup_web.py:1150-1200`).
5. `_handle_test()` dispatch (`setup_web.py:1933-1981`) → `refiner.test_provider(provider, api_key, model, api_base)`.
6. `_handle_save()`'s whitelist `if provider not in ("meta","ollama","openrouter","gemini","none")` (`setup_web.py:2022-2028`).

`refiner.test_provider()` is a flat `if`-chain keyed on provider name (`refiner.py:699-773`).

### Could an OAuth provider fit cleanly?

**Mechanically, yes — better than it first appears.** The `meta` provider already establishes the pattern: a bespoke client class that owns a `requests.Session`, sets its own auth header, posts `stream:false` JSON to a `{base}/responses`-style path, and returns extracted text. A `chatgpt` provider would be a near-copy of `_MetaClient` with:

- base URL `https://chatgpt.com/backend-api/codex`
- headers `originator`, `ChatGPT-Account-Id`, `session_id` (and whatever `OpenAI-Beta` value the endpoint wants)
- a bearer token read from a token store rather than a static env var
- a refresh-before-use step, and a 401 → refresh → retry-once step (mirroring Codex's `UnauthorizedRecovery` state machine: reload → refresh → give up)

**But four things do not fit the current shape and would be genuinely new machinery:**

1. **Credential lifecycle.** `effective_api_key() -> str` assumes immutability. You'd need either a callable/refresh-aware auth object, or a `TokenStore` class (read/write `~/.odicto/…` or keyring, expiry tracking, refresh, rotation handling). This is the single largest new subsystem — and the "refresh token rotation logs one of the two clients out" hazard documented by OpenClaw is a real user-facing bug you'd inherit.
2. **A localhost OAuth callback server.** `setup_web.py` already runs an HTTP server (`SETUP_PORT = 8765`, `config.py:89`), so the natural home is a `/oauth/callback` route on the setup page. But the redirect URI must be `http://localhost:1455/auth/callback` **or** nothing works, because the client_id is bound to an allow-list ("*Keep in sync with the Codex CLI Hydra redirect URI allow-list*"). Port 1455 is also what a user's actual `codex` CLI wants — collisions and the CLI's `/cancel` probe become your problem. You also need loopback-only binding and a state/PKCE implementation.
3. **Hard-coded third-party client identity.** You would be shipping someone else's `client_id` (`app_EMoamEEZ73f0CkXaXp7hrann`) and impersonating `originator: codex_cli_rs` to look like a first-party client, or inventing your own `originator` value and hoping the endpoint accepts it. The first is credential/identity impersonation of another client; the second is unverified and may simply 403.
4. **Model catalogue churn.** The subscription model list is curated, plan-gated, and moves (retirements announced against ChatGPT sign-in only). Odicto's `*_MODEL` + `*_MODEL_HISTORY` UX assumes a stable slug; a subscription provider would need a "whatever your plan currently serves" mode, and would silently break when a model retires.

### Effort estimate (engineering only, no policy risk priced in)

| Workstream | Rough size |
|---|---|
| `TokenStore` (PKCE, authorize URL, code exchange, refresh, expiry, rotation, secure storage) | 1–2 days |
| Loopback OAuth server + callback route in `setup_web.py` (state validation, error pages, port 1455/fallback) | 0.5–1 day |
| `_ChatGPTClient` in `refiner.py` (headers, request body, response extraction, 401→refresh→retry) | 0.5–1 day |
| `config.py` wiring: new provider in the `Literal`, `validate()` set, `ENV_DEFAULTS` (`CHATGPT_*` keys), `effective_llm_api_base()`, `effective_api_key()`/token path, `explain()` rows, `config_warnings` compatibility | 0.5–1 day |
| `setup_web.py` wiring: `EDITABLE_KEYS`, `_SECRET_KEYS`/`_mask_key`, `validate_provider_requirements`, select-menu HTML, `_handle_test`, `_handle_save` whitelist, `.env.example` lines (tests enforce parity) | 0.5–1 day |
| Tests (`test_units.py` / `test_pipeline.py` conventions) + docs | 0.5–1 day |
| **Total for a "works on my machine" version** | **~4–7 days** |
| **Total to a maintainable, supported feature** | materially more — the credential-refresh edge cases, rotation conflicts with the user's own `codex` CLI, and the absence of any contract all land in ongoing maintenance |

Note the repo's own discipline: `ENV_DEFAULTS`, `.env.example` and tests are kept in agreement, so "just add a provider" is not a one-file change here.

---

## Feasibility verdict + effort estimate + risks

### Verdict

| | |
|---|---|
| **Can it be built?** | Yes. `~4–7 days` for a working implementation, following the existing `_MetaClient` pattern. |
| **Is it official?** | No. No documented OAuth program, no published contract, no client registration. |
| **Is it permitted?** | Not stated either way. The ToS does not name it; the ToS does reserve suspension for circumventing protections. **No documented bans found, but no safe harbour either.** |
| **Is it stable?** | No. Credential gating is tied to client identity/version; models retire; the endpoint has no published semantics. |
| **Should Odicto ship it?** | **Recommendation: no, not as a supported provider.** |

### Risks, in order of severity

1. **Account risk to the *user*, not Odicto.** If OpenAI decides a non-Codex `originator` using subscription tokens is credential reuse, the harm lands on the user's ChatGPT account, not on Odicto. Shipping that as a first-class button in a setup page is putting users' accounts behind an undocumented bet.
2. **Silent breakage.** A single server-side change (as in [#33969](https://github.com/openai/codex/issues/33969)) turns the provider into a permanent graceful-degradation path — which in Odicto's design means silently pasting raw transcripts. Users will not understand why "AI mode stopped working".
3. **Impersonation.** Making it work probably requires shipping OpenAI's `client_id` and pretending to be `codex_cli_rs`. That is a different posture from "we implemented a documented flow", and it is the part most likely to be treated as circumvention.
4. **Model availability drift.** Anything you advertise will rot; some models are explicitly `API Access: false` and retirements are announced only for ChatGPT sign-in.
5. **Token-handling blast radius.** OAuth refresh tokens are long-lived and, per OpenAI, `~/.codex/auth.json` should be *"treat[ed] like a password"*. Storing them inside Odicto (a `.env` file that `setup_web.py` reads and a web page that can display masked values) widens the secret surface. Refresh-token rotation can also log the user out of their real Codex CLI.

### Legitimate alternatives for a subscriber who doesn't want a second bill

Ranked by how defensible they are:

1. **(A) Official Codex CLI as an external tool.** `codex login` → browser flow → the user's own Plus/Pro plan pays. Documented, sanctioned, first-party. Odicto could *document* it (not wrap it) as "if you have a ChatGPT plan, you can also use Codex CLI for coding tasks". If Odicto ever wanted to shell out, note the doc guidance: *"Use API key authentication for programmatic Codex CLI workflows, such as CI/CD jobs. Don't expose Codex execution in untrusted or public environments."* — and the honest caveat that a full agent CLI is the wrong shape for a one-shot text refiner (the [#36886](https://github.com/openai/codex/issues/36886) author reached the same conclusion about `codex exec`).
2. **(A) Enterprise `CODEX_ACCESS_TOKEN`.** Officially supported *for Enterprise workspaces only*: `printenv CODEX_ACCESS_TOKEN | codex login --with-access-token`. Explicitly *"intended for trusted scripts, schedulers, and private CI runners"*, and OpenAI still says *"For general OpenAI API calls, continue to use Platform API keys."* Not a general consumer answer, but the only *published* non-interactive subscription credential — worth **detecting and documenting** rather than reimplementing.
3. **(A) Official Codex app server / MCP surfaces.** `codex mcp-server` exists but is now **deprecated** in favour of the [Codex app server](https://developers.openai.com/codex/app-server) ([MCP server docs](https://developers.openai.com/codex/mcp-server)); there is also a [Codex SDK](https://developers.openai.com/codex/codex-sdk) and a [Codex plugin for Claude Code](https://github.com/openai/codex-plugin-cc). These are the officially-supported ways to drive Codex programmatically — but they are agent-shaped, not model-shaped, so they don't fit a "clean up my dictated sentence" call.
4. **(A) Status quo.** Odicto's existing `openrouter` / `meta` / `gemini` / `ollama` providers, all with plain keys, all stable, all documented. If the real goal is "subscribers shouldn't pay twice", the truthful answer today is: *they can't use their ChatGPT plan for this, and that is OpenAI's call, not ours.*

### Recommendation

**Do not implement ChatGPT-subscription OAuth as an Odicto provider.** Instead:

1. **Keep AI mode on API keys.** It is stable, documented, and the failure mode is a clear "your key is bad" message.
2. **If you want to serve subscribers, document the official path, don't reimplement it.** A short help note: "Odicto AI mode needs an API key. If you have a ChatGPT Plus/Pro plan, OpenAI's own Codex CLI can use that plan for coding tasks — Odicto doesn't use your ChatGPT account." This is honest, zero-risk, and gets the user to a working setup.
3. **Optionally, register a public question.** OpenAI has an open, unanswered canonical issue for exactly this ask ([#36886](https://github.com/openai/codex/issues/36886)) and a staff member who said they'd "pass it along" about docs ([community 1380525](https://community.openai.com/t/login-with-chatgpt-instead-of-bring-your-own-token/1380525)). If OpenAI publishes a contract, revisit — the code in `refiner.py`/`config.py` is pluggable enough that the implementation cost then is the same ~4–7 days, with a legitimate basis.
4. **If a future product decision forces it anyway**, make it an explicitly-labelled, opt-in "unsupported / may break / may affect your ChatGPT account" toggle; never a default; never the failure path of last resort; store tokens outside `.env`; and detect Enterprise `CODEX_ACCESS_TOKEN` as the preferred path.

---

## Sources

### Legend

- **(A) Officially documented / first-party** — from OpenAI's own policy pages, official docs, or the code of OpenAI's own first-party clients.
- **(B) Technically possible but not documented / not permitted** — real mechanism, no published contract; read from a public open-source repo or demonstrated empirically by a third party.
- **(C) Unverified / project-level claim** — asserted by a third party about OpenAI, not confirmed by OpenAI.

### OpenAI official policies

| # | Source | Tier | Used for |
|---|---|---|---|
| 1 | [Terms of Use (rest of world), eff. 2026-01-01](https://openai.com/policies/row-terms-of-use/) | A | credential-sharing clause; "what you cannot do" (modify/copy/distribute, reverse engineer, automated extraction, circumvent rate limits/protective measures); suspension rights |
| 2 | [Europe Terms of Use, updated 2026-01-16](https://openai.com/policies/terms-of-use/) | A | EU/EEA/UK/CH variant; OpenAI suspension rights; business-use addendum |
| 3 | [Usage Policies, eff. 2025-10-29](https://openai.com/policies/usage-policies/) | A | "breaking or circumventing our rules and safeguards may mean you lose access"; "circumventing our safeguards" |
| 4 | [OpenAI Services Agreement, eff. 2026-01-01](https://openai.com/policies/services-agreement/) | A | scope (API / Enterprise / Business); §3.3(g)(h)(i) API-key transfer, circumvention of rate limits and Usage Limits |

### OpenAI official docs & help

| # | Source | Tier | Used for |
|---|---|---|---|
| 5 | [Authentication — Codex docs](https://developers.openai.com/codex/auth) (→ `learn.chatgpt.com/docs/auth`) | A | the two sign-in methods and which clients support them; `~/.codex/auth.json`; token auto-refresh; `cli_auth_credentials_store`; `chatgpt_base_url`; device-code login; Enterprise `CODEX_ACCESS_TOKEN`; "for general API calls use Platform API keys" |
| 6 | [Models — Codex docs](https://developers.openai.com/codex/models) (→ `learn.chatgpt.com/docs/models`) | A | model availability depends on sign-in method/client; per-model `API Access` flags; retirements apply to ChatGPT sign-in only |
| 7 | [Use Codex with your ChatGPT plan (help #11369540)](https://help.openai.com/en/articles/11369540-codex-in-chatgpt) | A | "Sign in with your ChatGPT account"; plan range; which terms govern; usage limits are shared with Work |
| 8 | [openai/codex README](https://github.com/openai/codex/blob/main/README.md) | A | "Sign in with ChatGPT" for Plus/Pro/Business/Edu/Enterprise; Apache-2.0 licence |
| 9 | [Running Codex as an MCP server](https://developers.openai.com/codex/mcp-server) (→ `learn.chatgpt.com/docs/mcp-server`) | A | `codex mcp-server` **deprecated** in favour of the app server; alternative integration surfaces |
| 10 | [API keys dashboard](https://platform.openai.com/api-keys) | A | the documented credential for `api.openai.com` |

### OpenAI first-party source code (public, Apache-2.0)

| # | Source | Tier | Used for |
|---|---|---|---|
| 11 | [`codex-rs/login/src/server.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/server.rs) | B | issuer; authorize URL + params + scopes; redirect URI and ports 1455/1457 with the "Hydra redirect URI allow-list" comment; code exchange; `refresh_token` JSON body; revoke URL; token-exchange → `openai-api-key`; `access_denied` / `missing_codex_entitlement` |
| 12 | [`codex-rs/login/src/auth/manager.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/auth/manager.rs) | B | `CLIENT_ID = app_EMoamEEZ73f0CkXaXp7hrann`; `REFRESH_TOKEN_URL`; refresh-window and error taxonomy; `UnauthorizedRecovery` state machine; `chatgpt-account-id` header read |
| 13 | [`codex-rs/login/src/token_data.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/token_data.rs) | B | `https://api.openai.com/auth` claims incl. `chatgpt_plan_type`, `chatgpt_user_id`, `chatgpt_account_id`, `chatgpt_account_is_fedramp` |
| 14 | [`codex-rs/login/src/auth/storage.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/auth/storage.rs) | B | `auth.json` schema; file/keyring/auto/ephemeral modes |
| 15 | [`codex-rs/login/src/pkce.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/pkce.rs) | B | PKCE verifier/challenge generation (S256) |
| 16 | [`codex-rs/login/src/auth/default_client.rs`](https://github.com/openai/codex/blob/main/codex-rs/login/src/auth/default_client.rs) | B | `originator` default `codex_cli_rs`; User-Agent composition; `x-openai-internal-codex-residency`; `is_first_party_originator()` |
| 17 | [`codex-rs/app-server-protocol/schema/json/ChatgptAuthTokensRefreshParams.json`](https://github.com/openai/codex/blob/main/codex-rs/app-server-protocol/schema/json/ChatgptAuthTokensRefreshParams.json) | B | `chatgpt_account_id` / `previousAccountId` semantics |
| 18 | [`codex-rs/agent-identity/src/lib.rs`](https://github.com/openai/codex/blob/main/codex-rs/agent-identity/src/lib.rs) | B | `https://chatgpt.com/backend-api/codex` = Production backend base URL; `/backend-api` routing for JWKS |
| 19 | [`codex-rs/core/src/client_common.rs`](https://github.com/openai/codex/blob/main/codex-rs/core/src/client_common.rs) | B | streamed event protocol coupling (`ResponseStream`, `ResponseEvent`) |

### Third-party projects (their own docs/repos = primary for *their* claims)

| # | Source | Tier | Used for |
|---|---|---|---|
| 20 | [OpenCode — Providers docs](https://opencode.ai/docs/providers/) | B | ships first-class "ChatGPT Plus/Pro" OAuth via `/connect`; forwards compute-residency value from the credential; states "Anthropic explicitly prohibits this" and lists "ChatGPT Plus" among subscriptions usable "with zero setup" |
| 21 | [OpenClaw — `docs/concepts/oauth.md`](https://github.com/openclaw/openclaw/blob/main/docs/concepts/oauth.md) | C (about OpenAI) / B (about the flow) | claims OpenAI Codex OAuth is "explicitly supported" outside the CLI; documents the same issuer/scope/loopback-callback/accountId extraction; documents refresh-token rotation causing mutual logouts |
| 22 | [`numman-ali/opencode-openai-codex-auth`](https://github.com/numman-ali/opencode-openai-codex-auth) | C | community plugin for the same flow; self-labelled "personal development use only" |

### Statements, issues, and enforcement evidence

| # | Source | Tier | Used for |
|---|---|---|---|
| 23 | [@sama on X, 2026-05-01](https://x.com/sama/status/2050357911915028689) | A (statement by OpenAI's CEO) | "you can sign in to openclaw with your chatgpt account now and use your subscription there!" — a public blessing of one named third-party tool; not a policy |
| 24 | [openai/codex#36886](https://github.com/openai/codex/issues/36886) | A (OpenAI repo) / C (the body's technical detail) | canonical unanswered ask for a documented contract; open, 0 comments; documents the observed header set and the working one-shot request |
| 25 | [openai/codex#24971](https://github.com/openai/codex/issues/24971) | A | same question for native mobile clients; closed 0 comments, locked |
| 26 | [openai/codex#33969](https://github.com/openai/codex/issues/33969) | A | `403 invalid or disabled credential` after a version bump; evidence the endpoint's credential gating shifts |
| 27 | [openai/codex#40564](https://github.com/openai/codex/issues/40564) | A | user on a third-party token plan hitting rate-limit/usage surprises |
| 28 | [openai/codex#39272](https://github.com/openai/codex/issues/39272) | A | hardcoded first-party model IDs (`gpt-5.6-luna` / `gpt-5.6-terra`) in Codex internals |
| 29 | [OpenAI Developer Community thread 1380525](https://community.openai.com/t/login-with-chatgpt-instead-of-bring-your-own-token/1380525) | A-adjacent (OpenAI-hosted; staff/moderator poster) | "There are no official docs according to my best knowledge"; "I'll pass it along" re docs; thread closed with no answer |
| 30 | [OpenAI Developer Community thread 1378506](https://community.openai.com/t/login-with-chatgpt-allow-users-to-use-their-own-plus-subscription-in-3rd-party-apps/1378506) | A-adjacent | feature request for official third-party ChatGPT login; auto-closed, 0 replies |

### Explicitly *not* established from a primary source

- Any OpenAI statement naming third-party ChatGPT-OAuth reuse as permitted or prohibited. **(C — absent)**
- Any documented OpenAI account suspension, ban, or rate-limit clamp applied for this specific behaviour. **(C — none found; absence of evidence, not evidence of permission)**
- Whether the `chatgpt-account-id` / `OpenAI-Beta: responses=v1` / `session_id` / `version` header set is required, tolerated, or ignored — this comes from third-party observation ([#36886](https://github.com/openai/codex/issues/36886), [OpenClaw docs](https://github.com/openclaw/openclaw/blob/main/docs/concepts/oauth.md)), not from OpenAI source. **(C)**
- Whether the `requested_token=openai-api-key` token-exchange result is an ordinary usable platform API key. **(C — untested here)**
- Whether Cline / Continue / LibreChat / Aider / Zed ship ChatGPT-subscription login. Not verified; do not assume either way. **(C)**
