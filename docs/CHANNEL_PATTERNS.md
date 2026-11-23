# Channel patterns: the CLI and Telegram

Front doors that are clients of the chat API, signed in as a real person (specs/020, 021). Numbers are shared with the other pattern documents ([index](README.md#pattern-numbers)). How they sign in is [ACCESS_PATTERNS.md](ACCESS_PATTERNS.md).

| # | Lesson |
|---|---|
| 42 | Telegram channel |
| 89 | Every front door runs its turns on the queue, through the front door |
| 91 | A client that signs in for people must not hold a credential that can create them |
| 92 | A button in a chat approves only when the person it was shown to presses it, once, in time |
| 93 | Acknowledge an update when it is durable, not when it is answered |
| 94 | Linking an account takes the person's own login |

### 42. Telegram channel
`app/adapters/inbound/channels/telegram/` resolves a stable conversation and person per chat, runs each message as a turn through the front door (89) and collects the events into one reply. It reaches the public internet by design; `TELEGRAM_BOT_TOKEN` is empty by default and `run()` refuses to start without one. Approvals are buttons (92), updates are acknowledged when durable (93), accounts link by the person's own login (94).
- **Bug:** the long-poll `offset` was a local variable, so a restart redelivered every update, each a duplicate turn. It is durable in Redis and saved **after** handling.
- **Bug:** a turn with no text sent no message, which read as the bot ignoring the user; a fallback is substituted.

### 89. Every front door runs its turns on the queue, through the front door, and "nobody can answer an approval" is a property of the request
The CLI and Telegram once ran the graph in their own process, a second path that the worker's lock, crash reclaim and dead-letter archive never reached, and then published to the queue with an identity they built, so the door's limits and revocation never applied. Both are now clients of `POST /chat/stream/queued`, `/chat/resume` and `/chat/cancel` through the door (`channels/door.py`); nothing but the API writes to the queue (`test_channels_use_the_door.py` fails the build otherwise).
- **Unattended is a field of the request:** it rides on the job, so it survives a reclaim, and the worker declines a pause instead of forwarding it. The API accepts it from any caller because it can only make a turn stricter.
- **A redelivered update would be a second turn.** The channel sends the update number as `replay_key` (`telegram:<domain>:update:<id>`), claimed for `RESULTS_STREAM_TTL_SECONDS` and given back if the publish fails.
- **A conversation id that names its owner:** a `telegram:` id is claimable only by the caller it names, so each person in a group chat has their own conversation.
- **The CLI cancels on the way out.** Every refusal becomes one `error` event (`channel_door_refusal_total`); when the door is unreachable nothing falls back to the queue. Rollout: workers first.
- **Gaps:** the dedup claim lasts 300 s; `scripts/tools/ops_investigate.py` still builds its own one-shot graph.

### 91. A client that signs in for people must not hold a credential that can create them
The bot has to *be* each Telegram user at the authorization server. An application credential that creates a person on a first message was measured (Casdoor 4.24.0) and is wrong: the client id and secret administer the whole organization, so a leaked bot environment would take over every person in the tenant.
- **Derive, don't store:** a password is `HMAC(TELEGRAM_PERSON_SECRET, tenant, id)`, so a restart gives the same people and the bot keeps nothing per person.
- **Who creates a person:** an operator (`make telegram-person ACTION=add TELEGRAM_ID=<id>`); `TELEGRAM_AUTO_PROVISION` is an opt-in that adds a tenant-wide credential to the bot's environment, so use a tenant of its own.
- **Disabled is final;** a wrong password is never retried (five lock a person). The sign-in is the headless authorization-code flow, because the password grant ignores `resource` and the door refuses its token.
- **Gap:** that password also signs in at the web page and the MCP sign-in, so a leaked secret works as those people anywhere in the tenant.

### 92. A button in a chat approves only when the person it was shown to presses it, once, in time
Five ways Approve / Reject buttons become a hole (Principle II), each a test (`channels/telegram/test_bot.py`):
- **A group chat shows them to everyone:** the record names the Telegram user it was shown for; another tap is refused and the approval stands. The API repeats the check for the signed-in person.
- **A double tap or redelivered callback must not resume twice:** the first tap claims the approval (`SET NX`); a decision the door refused gives the claim back.
- **A late tap must not cancel anything** (it would stop a turn that is running by then), so it only says it expired.
- **An unanswered approval strands the conversation:** the record expires (`TELEGRAM_APPROVAL_TTL_SECONDS`) and the next message cancels the stale pause, waits for that job and resends under a new replay key.
- **Buttons that cannot be shown cannot be answered:** if the keyboard message fails, the pause is cancelled at once.
- `callback_data` is a random token, never the subject. `TELEGRAM_APPROVALS=false` is the old decline-unattended behaviour.
- **Gaps:** expiry is lazy (buttons stay on screen); not run against real Telegram.

### 93. Acknowledge an update when it is durable, not when it is answered
One sequential loop meant one slow turn delayed every chat, `/cancel` could not exist, and a webhook would be redelivered.
- **Both transports only push:** long-polling and the webhook put the update on a Redis list and acknowledge once it is there; `telegram/inbox.py` moves it to a `processing` list and removes it when handled.
- **Parallel across chats, in order within one,** bounded by `TELEGRAM_MAX_CONCURRENT_CHATS`; `/cancel` routes around the chat's queue.
- **Crash-safe, not forever:** in-flight updates return to the front with an attempt counted; past `MAX_ATTEMPTS` (3) one is dropped and counted (alert), so a poison update cannot loop the bot.
- **Webhook:** `X-Telegram-Bot-Api-Secret-Token` compared in constant time (403 otherwise); Redis down is a 503 so Telegram retries; the proxy must NOT require the door's token on `/telegram/*`.
- **Gaps:** one bot process per domain; the webhook was never called by real Telegram.

### 94. Linking an account takes the person's own login, and the bot keeps only what it can afford to lose
`/login` binds a Telegram id to the account the person signs in as, in their own browser; the bot never sees a password.
- **State is single use, short lived and bound to the asking Telegram id.** A confirmation page exists for one attack: someone sends THEIR link to a victim and the attacker's Telegram ends up bound to the victim's account; binding happens only on that button.
- **Only a refresh token is kept, sealed with Fernet (`TELEGRAM_LINK_KEY`).** Each turn refreshes it into a token the door verifies, so a disabled person is cut off at the next refresh. The tenant comes from the token (Principle I). Refreshes of one person are serialized, since a rotating server would otherwise revoke the link.
- `/login` is private-chat only; the pages set `no-store`, no referrer and a locked-down CSP.
- **Gaps:** whoever controls the linked Telegram account acts as the person (SIM swap); `/login` is not rate limited by the bot; losing `TELEGRAM_LINK_KEY` unlinks everyone; refresh-token rotation was not measured against real Casdoor.
