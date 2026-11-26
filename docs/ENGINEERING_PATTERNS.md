# Engineering patterns: settings, tests, builds and measurements

Lessons about how this repository is built and checked, not about the agent. Numbers are shared with the other pattern documents ([index](README.md#pattern-numbers)); 86 and 87 are also used in [ACCESS_PATTERNS.md](ACCESS_PATTERNS.md) and [TOOL_PATTERNS.md](TOOL_PATTERNS.md), so cite these two as `ENGINEERING_PATTERNS.md 86`. The billing parts of 59 and 60 are in [`packages/billing/docs/PATTERNS.md`](../packages/billing/docs/PATTERNS.md).

| # | Lesson |
|---|---|
| 40 | Eval statistical rigor |
| 48 | Real-backend testing at every layer |
| 59 | A settings error must not print the secret it rejected |
| 60 | A test that re-applies a migration must be the one that gives up |
| 69 | A model decision is made by a frozen evaluation |
| 72 | A security gate that cannot be satisfied gets a dated, reasoned exception |
| 75 | A topic matcher's cutoff belongs to a model |
| 76 | Images are built in stages, carry no installer, and run hardened |
| 77 | A swappable interface: speak its wire shape, declare every deviation |
| 83 | Compose interpolates the whole file on every command |
| 86 | A smoke check that shares state with its previous run passes once |
| 87 | A live test that rests on what a 3B model infers fails for the model |
| 88 | Four ways a check passes without checking |
| 90 | A dependency rule that is not an import scan is a wish |
| 95 | A setting that looks like "off" can mean "always" |
| 100 | A cache and a queue cannot share an eviction policy |
| 101 | A backup is proved by a restore |
| 102 | A pool's "check on checkout" can turn a restart into a stall |
| 106 | A trigger filtered by branch name lets a fork deploy |

### 40. Eval statistical rigor
`scripts/checks/eval.py` has two gates: each case runs `EVAL_REPETITIONS` (5) times and passes only if `REPETITION_PASS_THRESHOLD` (80%) pass (a stochastic system graded once is a coin flip), and a grounded-claims gate over the whole set (at least 95% of markers real), computed from the runtime's own counts, never a model's opinion of itself. The thresholds are demo defaults.

### 48. Real-backend testing at every layer
`tests/containers.py` `ensure_*()` helpers start ephemeral testcontainers, so `tests/integration/` and `tests/live/` run in CI while a no-Docker `pytest -q` stays fast. `promptfoo/` and `garak/` target the same small Ollama model.
- **xdist:** shared containers need a fixed cache dir, and `AsyncPostgresSaver.setup()` runs once under a lock.
- **Small local models make poor judges and targets.** garak's `dan` probe succeeded 100% on `qwen2.5:1.5b`, so `app/core/moderation.py` is the real defence. The judge is hosted with failover (`tests/deepeval/fallback.py`), the target local; deepeval is `flaky=True` in CI, so only a crash fails the job.
- **Bug:** a session-scoped stack leaked a saved memory into a later test, which made the 3B model propose `add_note` and pause for an approval nobody could give (~40 CI runs). An autouse fixture deletes each test's memories.
- **A gate must fail for a reason a change can cause.** Tests that need a 3B model to chain tool calls are `@pytest.mark.advisory` and run in a non-blocking step; `-m "e2e and not advisory"` is the gate. Trade-off: a regression only in skills or subagents shows in that step's log.
- Tests wait for the page's "turn over" signal, not the text, and a failed browser test prints the page transcript. `faulthandler_timeout = 300` names the line of a stalled test.

### 59. A settings error must not print the secret it rejected
Pydantic's `ValidationError` prints the raw value, a `Settings()` error is raised at import time, and the output goes to a log: a rejected API key was repeated whole.
- **One line closes it for every field:** `hide_input_in_errors=True` on the settings class; a test per path fails without it.
- **Credentials are `SecretStr`, and a blank is "not set"** (an empty `.env` variable arrives as `""`).
- A setting nothing reads is a promise the code does not keep, so a removed one has a test pinning its absence; a `.env.example` warning that can go stale has a test that fails the day it does.

### 60. A test that re-applies a migration to a shared database must be the one that gives up
A "can be applied twice" test failed about one parallel run in seven, once with an unrelated victim (`DeadlockDetected`). Re-running DDL is not harmless while other xdist workers write: a no-op `ADD COLUMN IF NOT EXISTS` takes an **ACCESS EXCLUSIVE** lock.
- **Why the wrong test failed:** Postgres aborts the session whose deadlock check runs first, the one that began waiting first, and that was the DML test.
- **Fix:** `tests/integration/schema_reapply.py::reapply` sets a `lock_timeout` (300 ms) below `deadlock_timeout`, so the script gives up and retries with jitter before any deadlock check. `DeadlockDetected` is deliberately not retried, and every re-apply is bounded by `asyncio.wait_for`, so a regression is a named failure, not a hang.
- **Same cause, second face:** `DID NOT RAISE UniqueViolation`, because the deadlock killed the FIRST write. Setup calls assert the setup did not fail. 16 of 16 parallel runs clean, against ~1 in 7 failing.
- **Gap:** other tests that run DDL are covered only if they use the helper (`.claude/rules/testing.md`).

### 69. A model decision is made by a frozen evaluation, and a benchmark number is only valid on the hardware and the text it was measured on
`make model-eval` (`scripts/model_eval/`, `evals/`, [`evals/README.md`](../evals/README.md)) reports a candidate next to the recorded result of the model in use, on this repository's own questions, with confidence intervals.
- **Frozen data, immutable baselines:** a manifest records every file's sha256; `dev` is disjoint from `test` and refused for reporting; a baseline cannot be overwritten without `FORCE=1`; one on an unreviewed set is `provisional`.
- **Controls:** every report runs the baseline against itself (must be `no_difference`) and a useless model (must be `worse`); a failure makes it `INVALID` (exit 5). A control is judged on the metrics that mean "it works" for that uselessness. A classifier's controls can fail because the labels carry no signal (the strong model's advantage was +0.032, so even a perfect router could not be told from a random one): check that before training anything.
- **`inconclusive` is not "equal":** `better`/`worse` only if the paired-bootstrap interval excludes zero (2,000 resamples, fixed seed, the SAME indices for both models).
- **Hardware is part of the number:** latency is stored per hardware id and never compared across ids. **Measure the real text:** a 110-token synthetic paragraph understated real 259-token chunks 2.4x.
- **Verified:** metrics against hand-computed values; 23 mutants, all caught in the end (5 survived first and were real gaps).
- **Gap:** the repository's own sets are small and unreviewed, so they separate good from useless but cannot rank two good models within a point.

### 72. A security gate that cannot be satisfied gets a dated, reasoned exception, and tests that keep the reasons true
CI's `trivy` job went red on every PR without a dependency change: its database learned of CRITICAL findings in already-pinned packages. A hard gate red for everyone is ignored by everyone, so fix it or except it visibly, never work around it.
- **Fix what can be fixed where it lives:** certifi was a lock bump, but a SECOND copy vendored in the base image's pip was out of the lock's reach (pip was later removed, 76). Run the official `aquasec/trivy` with the hard-gate flags before pushing: the same scan showed one finding in CI and three locally a day later.
- **When the only fix cannot be installed, bound it:** `.trivyignore.yaml` carried one entry per CVE with a `statement` (what the vulnerable call needs and why nothing here makes it) and an `expired_at`. Only the hard gates read it.
- **Check the exit whenever the gate next goes red,** not only on the date: it stayed red 16 runs while the exit was available for four, and a red gate nobody reads hid a second regression.
- **Today no exception is carried** (fastembed 0.9.1 widened its cap; pillow 13.0.0).

### 75. A topic matcher's cutoff belongs to a model, its examples to their text, and what it does with the unmatched is measured on real prompts
Label sets (`services/ml-service/labelsets/`, `POST /v1/decide`) match a text to a choice by embedding centroid.
- **A similarity cutoff is a property of the model:** 0.709 for bge-small, 0.247 for MiniLM on the same set. It lives in the set, per model; a model without an entry does not serve it; there is no default.
- **Store examples as text, not vectors:** a stored vector is valid for one model revision and fails as confident nonsense.
- **Measure before claiming:** the unmatched "almost always land on `smalltalk`" was six labels; a margin that separated right from wrong on authored sentences (0.010 vs 0.068) did not on real prompts (0.029 vs 0.028), and 11% of ordinary questions went up a tier. Read the router's output on real traffic before enabling it (`ROUTER_MODE=shadow`).
- **Choose the cutoff on one split, report on another** (`make labelset-calibrate` reads only `dev`). Tests that all pass first time are suspect: mutation found seven real gaps.

### 76. The images are built in stages with uv and a cache mount, carry no installer, and run hardened
An app image rebuild spent 347.7 s in `pip install`; the image carried pip, the base floated, and nothing restricted how containers run.
- **Four stages, the shipped one last:** `base`, `uv`, `builder` (dependencies into `/opt/venv`), `runtime`. Compose and CI's `image` job name `target: runtime`.
- **Pinned by digest, bumped by Dependabot;** the two Dockerfiles' digests move together (`test_dockerfile_pins.py`). Its 14-day cooldown does **not** apply to Docker tags, so a release's age is checked by hand; compose digests (LiteLLM) are bumped by hand (`make gateway-check`).
- **uv wheel cache mount, bind-mounted dependency file** (no layer holds it), `UV_COMPILE_BYTECODE=1`. Install step: 29.8 s cold, 11.4 s with one pin changed.
- **No installer ships:** the runtime stage uninstalls pip, setuptools and wheel and never copies uv (debug with `--target builder`).
- **Hardened at runtime:** `init: true`, `no-new-privileges`, `cap_drop: ALL`, `read_only: true`, size-capped tmpfs. Running it found `crawl4ai` creating `~/.crawl4ai` on **import** (so `$HOME` is a tmpfs) and a tmpfs `/tmp` being wrong for uploads (the api gets a disk-backed volume).
- **SBOM and provenance** are attached when CI's `image` job publishes on `main`.
- **CI found what no local run could:** `tests/containers.py` built the ML image with the SDK's legacy builder, which rejects `RUN --mount`. It uses the CLI (BuildKit) now. A build feature is only as portable as the least capable builder anyone uses.
- **No pytest file pins these flags as text** (it would fail on an edit, not a regression); Trivy and CI's `docker-build` read the files.
- **Gaps:** CI has no cache-mount speed-up; images are unsigned; tmpfs sizes are chosen, not measured; the hardening ran on the dev stack, not the droplet.

### 77. An interface you may swap for a hosted one: speak its wire shape, declare every deviation, and never fill a field you cannot honestly fill
Spec 016 made the ML service answer typed decisions in the shape of the hosted Jev interface, so a hosted implementation can answer later with no caller change.
- **Match documented fields exactly and prefix every extra** (`x_served_by`, `x_evidence`); the client reads documented fields only.
- **A required field you cannot honestly produce is left out and listed:** ours is a cosine similarity, not a `confidence`; a plausible number invites a threshold that means nothing. Absence is loud, a pseudo-probability is quiet.
- **What is not implemented is a 4xx that names it** (`score`, `noul`, `assets` and an object `state` are 422).
- **Say what was not observed:** the hosted interface was never called successfully (402 until a card is added), so every hosted field is the documentation's.
- **Gap:** nothing proves the two implementations agree.

### 83. Compose interpolates the whole file on every command, so a required variable in an opt-in profile breaks every other command
The MCP gateway services wrote `${CASDOOR_DB_PASSWORD:?...}`; it passed every hermetic test, and when started for real **every compose command failed for everyone who never starts that stack**.
- **Rule:** the dev compose file requires nothing and has no `${NAME:?`. A secret defaults to empty (`${VAR:-}`) and the service fails loudly itself; `make front-door-up` generates passwords into a private directory outside the repo and passes a second `--env-file`.
- A message that helps the person starting one service is a regression for everyone who is not.

### 86. A smoke check that shares state with its previous run passes once and fails the second time, and a mutation list kept runnable finds the test a review missed
- **State that outlives a run:** `make authlimit-smoke` asserts exact counts against a Redis token bucket that refills half a token a second, so a run 20 s after another failed on a count that was right a minute earlier. The stage now waits for the refill and ran twice back to back. Reset it, wait it out, or give each run its own key.
- **A mutation list you can re-run finds what a review does not:** `scripts/checks/authlimit_mutants.py` applies 26 deliberate defects one at a time and requires a test to fail. **One survived** (trusting a duplicated `X-Forwarded-Uri`: the test put the forged value after the real one). A stale target fails loudly instead of being skipped.

### 87. A live test that rests on what a 3B model infers fails for the model, not the code; a count of OK replies cannot see a call that is valid and wrong
`test-live` went red on one gate test: asked "Who is Ecorp's Support Lead?", CI's `qwen2.5:3b` called `query_employees(name_contains="Support Lead")`. The call was valid, the tool found nobody, nothing in the app was wrong.
- **A reproduction must use CI's Ollama and still shows a direction, not a result:** the wrong call reproduced only through `/api/chat` on CI's image; the same request through `/v1/chat/completions` returned no call.
- **Measure the levers first:** a larger context did nothing, `repeat_penalty 1.1` made it worse, clearer descriptions helped the 3B (0 to 2 of 9) and hurt the 7B (7 to 6), so the tool descriptions were not changed.
- **Ask the question the tool answers:** the test exists for browser, tool, Postgres, so it asks "Is there anyone named Dana at Ecorp?" (8 of 8).
- **The probe never saw it:** a pass count cannot tell a call that fits the schema from one that answers the question. `make probe-tools` now compares the arguments a question demands.
- **Gaps:** the fixture's Ollama is unpinned; the two multi-step tests stay advisory.

### 88. Four ways a check passes without checking, found while proving the API side of the front door
- **A reading that returns a default for "nothing":** `metric_value` answers 0 for a series with no point, which is also what a wrong implementation gives. Read every series from ONE collection and assert the side that is not zero.
- **A filter over a set that may be empty:** the smoke selected log lines by container name, compose prefixes them, the set was empty and "no JWT in the logs" passed. Check that the selection is not empty.
- **A double that cannot know what the real thing wants:** the page's sign-in tests passed on a DOM stub while a real browser got a 400. The real run is its own tier (`make authlimit-web-check`).
- **A comparison against a series that does not exist yet:** `errors > 0 and increase(ok) == 0` is empty before the first success, so the alert was silent when needed. `errors > 0 unless increase(ok) > 0` fires.

### 90. A dependency rule that is not an import scan is a wish
"`app/core` imports nothing from `agent`, `domains` or `api`" was written down and no test read an import. One AST pass found 40 violations in 26 files, each reasonable when written.
- **State the rule as a list of imports and keep it a ratchet:** `tests/core/test_architecture_layers.py` names the rings, infrastructure clients and adapters; violations went into a ledger that may only shrink (a listed one that is gone also fails). It started at 40 and ended empty.
- **A scanner needs its own tests** (including a function-local `import psycopg`). **An unbound port fails closed** (`PortNotBoundError`); `test_entrypoints_wire.py` and `test_bootstrap.py` catch a missing `wire()` or adapter; `tests/fakes/conformance.py` holds every fake to its Protocol.
- **Move code, do not rewrite it:** SQL and HTTP moved verbatim, so the existing tests kept their meaning.

### 95. A setting that looks like "off" can mean "always", and a tool that fails silently proves nothing until you know it was listening
PgBouncer and a profiler (specs/022) turned up four things, each found by running the real component.
- **`prepare_threshold=0` is not "never prepare"** in psycopg 3: it prepares every statement on its first run; only `None` disables (default 5). Behind a transaction pooler it gave `DuplicatePreparedStatement` within seconds. `POOLER_SAFE_CONNECTION_KWARGS` is the one definition; a scan fails the build on any other value.
- **A light run passes the unsafe default:** 60 queries showed nothing, 12 pools of 40 transactions failed ~15%. The integration test uses three server connections for thirty clients and carries its own control: the old settings must fail (88).
- **"It sent nothing" and "the server was not ready" look alike:** an empty Pyroscope read as a broken SDK, but the server needed ~3 minutes. Measure readiness before judging the client.
- **Hardening is read off a run** (`docker diff` and the log of a read-only container) and asserted in `tests/core/test_compose_pooler.py`.
- **Gaps:** nothing ran on the droplet; profiler overhead unmeasured; the session-state scan cannot see SQL assembled from pieces.

### 100. A cache and a queue cannot share an eviction policy, and turning persistence on is a migration
Production had one Redis for the queue, locks, cancel flags, dedup claims, the door's buckets and revocation list, Telegram state **and** the semantic cache (R15).
- **No policy serves both:** a cache wants `allkeys-lru`; the queue must never evict, and `volatile-lru` would evict exactly the keys with an expiry (locks, results streams, claims). The queue's Redis is `noeviction` with a `maxmemory` and the cache has its own; a full one REFUSES the write, so an 80% alert is part of the change. Without `maxmemory`, a limit is a kernel kill.
- **Switching persistence on for an existing volume starts it empty** (`--appendonly yes` ignores a `dump.rdb`), which here would have **un-revoked people**. Safe order: `CONFIG SET appendonly yes` in the RUNNING server, wait for the rewrite, then start the new service on the same volume.
- **A plain Redis cannot load a Stack server's AOF,** so both stay on the Stack image; it takes flags from `REDIS_ARGS`, not `command:`. An alert on a cache's memory is noise, so the rule watches the queue only.
- **Gaps:** nothing ran on a droplet; `maxmemory` values are defaults; no `requirepass`.

### 101. A backup is proved by a restore into a server that has never seen the data, and the first one finds what the backup could not
- **A new production volume could not start:** compose set `POSTGRES_DB: appdata`, so `02-appdata.sql` failed on `CREATE DATABASE`. Test helpers start the dev layout, so nothing had ever started production's Postgres on an empty volume. The drill and a regression test start the production service as defined.
- **Run the real script against a fake of its only outside world:** a stand-in `docker` on `PATH` ran `backup.sh` and found GNU-only `head -n -N`. A backup is complete only when its directory has lost `.partial`.
- **A snapshot does not carry the alias the app uses:** Qdrant snapshots are per physical collection, so the `-live` aliases are saved beside them; the test restores into a SECOND server and searches through the alias.
- **Verify the copy off the box:** a bucket copy is complete only when its manifest (sizes, SHA-256) exists and the download refuses a mismatch.
- **Gaps:** laptop numbers on small data; end-to-end recovery time unmeasured; nothing alerts that a backup stopped.

### 102. A pool's own "check on checkout" can turn a restart into a 30-second stall; replace the dead connections together instead
After a PgBouncer or Postgres restart every pooled connection is dead and `psycopg_pool` finds out one caller at a time (a pool of 18: up to 18 failed requests).
- **Reproduced:** terminating every backend of the role made the plain pool fail 6 of 10 and 11 of 22 queries.
- **`check=` made it worse:** the first request failed with `PoolTimeout` after 30 s, because the check loop sleeps a doubling backoff (1, 2, 4, 8, 16 s) per dead connection while replacements were added within a second. Read the library's loop before enabling a check whose cost you only estimated.
- **What works:** the pool's own `check()`, run once in the background when a used connection turns out `broken`, replaces every dead idle one: 2 failures at any size, none slow. `SelfHealingPool` does that for both pools, never for a deadlock or cancelled statement, one check at a time.
- **The test carries its own control** (88): the same scenario on a stock pool must fail.
- **Gaps:** the restart is simulated; a real PgBouncer restart was not exercised; `agent_db_pool_connection_lost_total{pool}` has no alert.

### 106. A trigger filtered by branch name lets a fork deploy
`deploy.yml` ran on `workflow_run` of CI with `branches: [main]` and checked only `conclusion == 'success'`.
- **The hole:** that filter matches the NAME of the branch the triggering run was on, and a pull request from a fork's own `main` reports `main`. The workflow then ran with this repository's token and secrets, checked out the fork's commit, built it and pushed it as `:latest`. Found by reading and by public runs of another repository; not exploited here. Never use `workflow_run` or `pull_request_target` to ship.
- **What replaced it:** ci.yml calls `deploy.yml` as its last job on a push to main (a manual run redeploys or rolls back, after checking the commit is on main). One run builds each image, proves it, scans it and publishes that build as `:<sha>`; `:latest` moves only after every job passed, and only for main's newest commit.
- **The gate that listed too little:** `needs` with `failure()` lets a timed-out or skipped job through, so `promote` names every job and requires each to be `success`; a test holds the list complete.
- **Gaps:** if a newer commit fails CI, the older good one is not released until the next merge (run Deploy by hand with its SHA). No automatic rollback: the release fails and prints the previous SHA. The production images are pinned by hand: Dependabot does not read these compose files. Nothing has deployed yet, so the droplet path is proven only against fakes.

