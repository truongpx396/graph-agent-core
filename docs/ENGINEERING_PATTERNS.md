# Engineering patterns: lessons about this repository's settings, tests, builds and measurements

Lessons that are not about the agent. They were written in [GRAPH_PATTERNS.md](GRAPH_PATTERNS.md) and moved here so that file stays about the graph, and **keep their original numbers** so every earlier reference resolves. The number sequence is shared with GRAPH_PATTERNS.md and [COST_GOVERNANCE.md](COST_GOVERNANCE.md), and 86 and 87 are also graph patterns there: cite these two as `ENGINEERING_PATTERNS.md 86`. The billing-specific parts of 59 and 60 are in [`packages/billing/docs/PATTERNS.md`](../packages/billing/docs/PATTERNS.md).

| # | Lesson |
|---|---|
| 59 | A settings error must not print the secret it rejected |
| 60 | A test that re-applies a migration to a shared database must be the one that gives up |
| 69 | A model decision is made by a frozen evaluation; a benchmark number is valid only on the hardware and text it was measured on |
| 72 | A security gate that cannot be satisfied gets a dated, reasoned exception, and tests that keep the reasons true |
| 75 | A topic matcher's cutoff belongs to a model, its examples to their text |
| 76 | The images are built in stages with uv and a cache mount, carry no installer, and run hardened |
| 77 | An interface you may swap for a hosted one: speak its wire shape, declare every deviation |
| 83 | Compose interpolates the whole file on every command |
| 86 | A smoke check that shares state with its previous run passes once and fails the second time |
| 87 | A live test that rests on what a 3B model infers fails for the model, not the code |
| 88 | Four ways a check passes without checking |
| 90 | A dependency rule that is not an import scan is a wish |
| 95 | A setting that looks like "off" can mean "always", and a silent tool proves nothing until you know it was listening |
| 100 | A cache and a queue cannot share an eviction policy, and turning persistence on is a migration |
| 101 | A backup is proved by a restore into a server that has never seen the data |
| 102 | A pool's own "check on checkout" can turn a restart into a 30-second stall |

## Patterns

### 59. A settings error must not print the secret it rejected
Pydantic's `ValidationError` prints the raw offending value, a `Settings()` error is raised at import time, and the output goes to a terminal or container log. With an API key set to a value its validator rejected, the startup error repeated the key whole.
- **One line closes it for every field:** `hide_input_in_errors=True` on the settings class. The error still names the field and the rule, not the value. A test per path fails without it.
- **Credentials are `SecretStr`, and a blank is "not set"** (a variable left in `.env` with no value arrives as an empty string). A real value is never inspected in the clear.
- **A setting nothing reads is a promise the code does not keep,** so a deliberately removed one has a test pinning its absence; a `.env.example` warning that can go stale ("do not enable X until Y exists") has a test that fails the day Y exists.

### 60. A test that re-applies a migration to a shared database must be the one that gives up
A "this migration can be applied twice" test failed about one parallel run in seven, once with an unrelated test as the victim (`DeadlockDetected`). Re-running DDL is not harmless on a database other xdist workers are writing to: a no-op `ADD COLUMN IF NOT EXISTS` takes an **ACCESS EXCLUSIVE** lock, `CREATE INDEX IF NOT EXISTS` a **SHARE** lock.
- **Why the wrong test failed:** a transaction holding one table and waiting for another forms a cycle with the script, and Postgres aborts whichever session's deadlock check runs first, the one that began waiting first: the DML test, not the DDL one.
- **Fix:** `tests/integration/schema_reapply.py::reapply` sets a `lock_timeout` (300 ms) below `deadlock_timeout`, so the script abandons a wait and rolls back before any deadlock check, then retries with jitter. `DeadlockDetected` is deliberately not retried. A test builds the cycle on purpose, and every re-apply is bounded by `asyncio.wait_for` so a regression is a named failure, not a hang.
- **The same cause wore a second face:** `DID NOT RAISE UniqueViolation`, because the deadlock killed the FIRST write so the second "succeeded". Setup calls now go through a helper that asserts the setup did not fail.
- 16 of 16 parallel runs clean, against about 1 in 7 failing before.
- **Gap:** other tests that run DDL against the shared database are covered only if they use the helper (`.claude/rules/testing.md` says so). The billing package keeps its own copy of the helper.

### 69. A model decision is made by a frozen evaluation, and a benchmark number is only valid on the hardware and the text it was measured on
Choosing an embedding, reranking or screening model from a model card ends with a model that is better on someone else's data. `make model-eval` (`scripts/model_eval/`, data in `evals/`, [`evals/README.md`](../evals/README.md)) instead reports the candidate next to the recorded result of the model in use, on this repository's own questions, with confidence intervals and a verdict.
- **Frozen data, immutable baselines.** A manifest records the sha256 of every file and a mismatch stops the run; `dev` (for tuning) is refused for reporting and is disjoint from `test`; a baseline cannot be overwritten without `FORCE=1`, and one recorded against an unreviewed set is `provisional`.
- **Controls:** every report carries the baseline against itself (must be `no_difference`) and a useless model (must be `worse`). If either fails the report is `INVALID` and the command exits 5. A control is judged on the metrics that mean "it works" for that kind of uselessness (a random screen has ~0.5 recall like the real one, so judging it on recall failed the committed baseline). A classifier's controls can fail because of the labels: the strong model's mean advantage was +0.032, so even a perfect router could not be told from a random one, and the report now says so (`WHY:`). Check that the labels carry a signal before training anything.
- **`inconclusive` is not "equal":** `better` or `worse` only if the paired-bootstrap interval excludes zero (2,000 resamples, fixed seed, the SAME indices for both models); a touching interval says how many more items would settle it.
- **Hardware is part of the number.** Latency is stored per hardware id and never compared across ids (on an ARM laptop int8 bge-small was *slower* than fp32; ONNX nomic on CPU is 3-5x slower than Ollama on Metal).
- **Measure the text the system really sees:** a 110-token synthetic paragraph understated the real 259-token chunks 2.4x, so the retrieval set is chunked by the app's own chunker.
- **Verified:** metrics are checked against hand-computed values and the harness is hermetic. 23 mutants, all caught in the end; 5 survived first and were real gaps (an inclusive cap, an interval that only touches zero called significant, the percentile, a "worse" control accepting `inconclusive`, a cached file's hash check).
- **Gaps:** the repository-own sets are small and unreviewed by a person, so they separate a good model from a useless one but cannot rank two good ones within a point; latency is one machine.

### 72. A security gate that cannot be satisfied gets a dated, reasoned exception, and tests that keep the reasons true
On 2026-10-21 CI's `trivy` job went red on every PR without any touching a dependency: its database had learned of CRITICAL findings in packages the lock already pinned (certifi, pillow). A hard gate red for everyone is ignored by everyone, so it must be fixed or deliberately and visibly excepted, never worked around.
- **Fix what can be fixed, where it lives.** certifi was a one-line lock bump, but the image also carried a SECOND certifi vendored inside the base image's pip, which the lock cannot reach (pip was later removed: 76). Read the scan's output, then the image.
- **Read the gate where it will run:** the same scan listed one finding in CI and three locally a day later, because the database moved. Run the official `aquasec/trivy` with the hard-gate flags before pushing.
- **When the only fix cannot be installed, say so and bound it.** Pillow's fix needed 13.0.0 and fastembed capped `pillow<13`. `.trivyignore.yaml` carried one entry per CVE with a `statement` (what the vulnerable call needs, and the evidence nothing here makes it) and an `expired_at` that brings it back and fails the gate again. Only the hard gates read the file; the full-report steps do not.
- **The reason was an expiry, not prose:** the gate stayed red 16 runs (until 2026-10-28) while the exit was available for four of them. A dated exception needs its exit condition **checked whenever the gate next goes red**, not only on the date; a gate red for a week has stopped being read, which hid a second regression (`test-live`, 10-24).
- **Today no exception is carried:** fastembed 0.9.1 widened the cap, the lock is on pillow 13.0.0 and certifi 2026.10.19, and the entries were deleted.

### 75. A topic matcher's cutoff belongs to a model, its examples to their text, and what it does with the unmatched is measured on real prompts
Spec 015 added label sets: a reviewed file of choices with example sentences, matched to a text by embedding centroid (`services/ml-service/labelsets/`, `POST /v1/decide`).
- **A similarity cutoff is a property of the model that produced the similarities:** the same set needed 0.709 for bge-small and 0.247 for MiniLM, and the service's fallback IS the other model. So the cutoff lives in the set, per model; a model without an entry does not serve the set; there is no default.
- **Store the examples as text, not vectors:** a stored vector is valid for one model revision and fails as confident nonsense. The service re-embeds when the model reloads under a different revision.
- **Measure before writing a claim about the unmatched.** A draft said they "almost always land on `smalltalk`"; counted, they land on six labels. A margin that separated wrong from right labels on the authored set (0.010 vs 0.068) did not on real prompts (0.029 vs 0.028), and 11% of ordinary knowledge-base questions went up a tier. Read the router's output on real traffic before turning it on: that is what `ROUTER_MODE=shadow` is for.
- **Choose the cutoff on one split, report on another,** and say which sentences informed an example (`make labelset-calibrate` reads only `dev`; a test fails on an example that is a near-duplicate of a real prompt).
- **Tests that pass first time are suspect:** a mutation check found seven real gaps in tests that all passed.
- **Gap:** the label table and rules are policy; nothing measured says the tiers they pick give better answers.

### 76. The images are built in stages with uv and a cache mount, carry no installer, and run hardened
Rebuilding the app image after a lock change spent 347.7 s in `pip install`. The Dockerfile was one stage with no cache, the image still carried pip (whose vendored certifi Trivy reads, 72), the Python base floated, and nothing restricted how containers run. This covers both production images (app and ML service).
- **Four stages, the shipped one last:** `base` (Python), `uv` (the installer), `builder` (dependencies into `/opt/venv`, thrown away) and `runtime` (base + OS packages + the venv copied to the same path + the app). Compose and `deploy.yml` name `target: runtime`.
- **Pinned by digest, bumped by a bot:** `base` and `uv` are each a `FROM` line Dependabot can bump (`.github/dependabot.yml`, Python minor never moved); the two Dockerfiles' digests move together (`tests/core/test_dockerfile_pins.py`). Dependabot's 14-day cooldown does **not** apply to Docker tags, so a release's age is checked by hand. Compose digests (LiteLLM, ...) are bumped by hand (`make gateway-check`).
- **A cache mount for uv's wheel cache, a bind mount for the dependency file** (no layer holds it; the step reruns exactly when the file changes). `UV_COMPILE_BYTECODE=1` because a non-root container cannot write `.pyc` under a root-owned venv.
- **No installer ships:** the runtime stage uninstalls pip, setuptools and wheel and never copies uv, so `docker exec ... pip install` no longer works (build `--target builder` to debug).
- **Hardened at runtime** in both compose files: `init: true`, `no-new-privileges`, `cap_drop: ALL`, `read_only: true`, writes to size-capped tmpfs, merged service by service and never applied to third-party services whose write paths nobody has checked. Running it found two things reading the code did not: `crawl4ai` creates `~/.crawl4ai` on **import** (so `$HOME` is a tmpfs owned by the app user), and a tmpfs `/tmp` is wrong for uploads (the multipart parser spools there; the api gets a disk-backed volume).
- **Published with an SBOM and provenance** (`deploy.yml`, not yet run: it runs on `main` after CI).
- **Measured** (arm64 laptop, one run each): install step 29.8 s cold, 11.4 s with one pin changed; app image 1.67 GB against 1.69 GB.
- **CI found what no local run could:** `tests/containers.py` built the ML image with the Docker SDK's legacy builder, which rejects `RUN --mount`. It builds with the CLI (BuildKit) now, and a test fails if any test calls `images.build(`. A build feature is only as portable as the least capable builder anyone uses: grep every way an image is built before adopting one.
- **No pytest file pins these flags as text:** a test that reads a Dockerfile for strings fails on an edit, not on a regression; Trivy's misconfiguration scan and CI's `docker-build` job already read them.
- **Gaps:** CI gets the smaller image but not the cache-mount speed-up; images are not signed; no Dockerfile linter; the lock has no hashes; tmpfs sizes were chosen, not measured under load; the hardening ran on the dev stack, not the droplet.

### 77. An interface you may swap for a hosted one: speak its wire shape, declare every deviation, and never fill a field you cannot honestly fill
Spec 016 made the ML service answer typed decisions in the request and answer shape of the TypeSafe System One / Jev interface, so a hosted implementation can answer later without changing any caller.
- **Match the documented fields exactly and prefix every extra** (`x_served_by`, `x_evidence`, `x_labelset_version`). The client reads documented fields only; a test feeds it an answer built from the hosted documentation.
- **A required field you cannot honestly produce is left out and listed, not filled.** Ours is a cosine similarity, not a `confidence` or `probabilities`; a number in the right range under the right name invites a threshold that means nothing. Absence is loud, a pseudo-probability is quiet.
- **What is not implemented is a 4xx that names it:** `score`, `noul`, `assets` and an object `state` are 422.
- **Say what was not observed:** the hosted interface was never called successfully (402 until the account has a card), so every hosted field in the contract is the documentation's, and the contract says so on its first page.
- **Gap:** nothing proves the two implementations agree; the swap checklist (privacy, spend the meter cannot see, abstention policy, one recorded response, an evaluation) is a list of decisions, not a feature.

### 83. Compose interpolates the whole file on every command, so a required variable in an opt-in profile breaks every other command
The MCP gateway services wrote `${CASDOOR_DB_PASSWORD:?run make mcp-gateway-up}`. It passed every hermetic test, and when the stack was started for real **`docker compose config`, `make up` and every other command failed for everyone who never starts that stack**: Compose interpolates the entire file before it looks at profiles.
- **The rule:** the dev compose file requires nothing to be set, and has no `${NAME:?`. A secret defaults to empty (`${VAR:-}`) and the service fails loudly on its own. `make mcp-gateway-up` generates the passwords once into a private directory outside the repo and passes them as a second `--env-file`.
- **Generalises to** every `${...}`: a message that helps the person starting one service is a regression for everyone who is not.

### 86. A smoke check that shares state with its previous run passes once and fails the second time, and a mutation list kept runnable finds the test a review missed
- **State that outlives a run.** `make authlimit-smoke` asserts exact counts against a per-tenant token bucket in Redis that refills at half a token a second, so a run 20 s after another found the tenant half empty and failed on a count that was right a minute earlier. The stage now waits for the refill and was run twice back to back. Any end-to-end check of something with memory (limiter, cache, counter) must reset it, wait it out, or use its own key.
- **A mutation list you can re-run finds what a review does not.** `scripts/checks/authlimit_mutants.py` applies 26 deliberate defects one at a time and requires a test to fail. The first run caught 22, three did not compile and **one survived:** trusting a duplicated `X-Forwarded-Uri` (the test appended the forged value AFTER the real one). The list is a file in the repo; a stale target fails loudly instead of being skipped.
- **Use when:** a security property is claimed in a spec and the only evidence is that the tests pass.

### 87. A live test that rests on what a 3B model infers fails for the model, not the code; a count of OK replies cannot see a call that is valid and wrong
`test-live` was red on `main` from 2026-10-24 on one gate test: asked "Who is Ecorp's Support Lead?", CI's `qwen2.5:3b` called `query_employees(name_contains="Support Lead")`, a job title in a name filter. The call is valid, the tool correctly finds nobody, nothing in the app was wrong.
- **A reproduction must use the Ollama CI uses, and still shows a direction, not a CI result:** CI's exact wrong call reproduced only through `/api/chat` on CI's image; the same request through `/v1/chat/completions` returned no call at all. Greedy decoding is not a fixed outcome for a borderline case (arm64 here, x86 in CI).
- **Measure the levers before changing anything:** on the 3B, role questions were 0 of 9 right; a larger context did nothing, `repeat_penalty 1.1` made it worse, clearer descriptions helped the 3B (0 to 2) and slightly hurt the 7B (7 to 6). So the product's tool descriptions were not changed.
- **Ask the question the tool answers.** The test exists for the browser, tool, Postgres round trip, so it asks "Is there anyone named Dana at Ecorp?" (8 of 8 correct), and the surname exists only in the database row, so seeing it in the answer still proves the round trip.
- **Why the probe never saw it:** an aggregate pass count cannot tell a call that satisfies the schema from one that answers the question. `make probe-tools` now compares the arguments a question demands (`Probe.args`).
- **Gaps:** the fixture's Ollama is unpinned; the 3B still cannot map a title to a department; the two multi-step tests stay in the advisory step.
- **Use when:** a gate test fails on a small model's answer while the tool call itself ran: reproduce under CI's Ollama first, change one lever at a time against more than one phrasing, and fix the question or the tool only for the lever that moved.

### 88. Four ways a check passes without checking, found while proving the API side of the front door
- **A reading that returns a default for "nothing":** `metric_value` answers 0 for a series with no point, and an OpenTelemetry gauge reports once per set. Reading two series with two calls found the first and read the second as 0, the value a wrong implementation also gives. Read every series from ONE collection and assert the side that is not zero.
- **A filter over a set that may be empty:** the smoke selected the API's log lines by container name, compose prefixes them `api-1  |`, so the set was empty and "no JWT-shaped string in the logs" passed. A check over a selection needs a check that the selection is not empty.
- **A double that cannot know what the real thing wants:** the page's sign-in tests passed on a DOM stub while a real browser got a 400 (the server wanted `resource` on the exchange too). The real run is a tier of its own (`make authlimit-web-check`).
- **A comparison against a series that does not exist yet:** `errors > 0 and increase(ok) == 0` is empty before the first success, so the alert was silent in the case it exists for. `errors > 0 unless increase(ok) > 0` fires.
- **Use when:** a test, smoke or alert was written to prove something is absent and has never been seen to fail.

### 90. A dependency rule that is not an import scan is a wish
The repository said "`app/core` imports nothing from `agent`, `domains` or `api`" and no test read an import. One AST pass found 40 violations in 26 files (the cost policy opened Postgres, the graph built its own OpenAI client, the security kernel imported `qdrant_client`, the exactly-once guard talked to Postgres), each reasonable on the day it was written.
- **State the rule as a list of imports, and keep the list as a ratchet:** `tests/core/test_architecture_layers.py` names the rings, the infrastructure clients and the adapter modules. Existing violations went into a ledger that may only shrink (a listed one that is gone also fails). It started at 40 and ended empty.
- **A scanner needs its own tests:** the table feeds it source strings and asserts exactly what it flags, including a function-local `import psycopg`.
- **An unbound port fails closed:** `Slot.get` raises `PortNotBoundError`; `test_entrypoints_wire.py` fails an entrypoint that never wires and `test_bootstrap.py` a port with no adapter.
- **A fake is trusted only if checked:** `tests/fakes/conformance.py` holds every adapter and fake to its Protocol.
- **Move code, do not rewrite it:** the SQL and HTTP moved verbatim, so the existing SQL-shape tests kept their meaning.
- **Use when:** a rule about what may depend on what is only written in prose, or a new external system is about to be imported from the inside.

### 95. A setting that looks like "off" can mean "always", and a tool that fails silently proves nothing until you know it was listening
Putting PgBouncer in front of Postgres and a profiler in every process (specs/022) turned up four things, each found by running the real component.
- **`prepare_threshold=0` is not "never prepare":** in psycopg 3 it prepares every statement on its first run; only `None` disables it (the default is 5). The checkpointer pool had said `0`, and produced `DuplicatePreparedStatement` behind a transaction pooler within seconds. `POOLER_SAFE_CONNECTION_KWARGS` is the one definition and a scan fails the build on any connection site that sets anything but `None`.
- **A light run passes the unsafe default:** 60 queries over three connections showed no error; 12 pools of 40 transactions failed ~15%. The integration test uses three server connections for thirty clients and refuses prepared statements outright, and carries its own control: the old settings must fail (88).
- **"It sent nothing" and "the server was not ready" look the same:** an empty Pyroscope read as a broken SDK, but the 2.x server took ~3 minutes to be ready and drops pushes until then. Measure readiness before judging the client (`-architecture.storage=v1 -write-path=ingester` is ready in 19 s).
- **Hardening is read off a run:** each new image went up read-only with no capabilities, and its `docker diff` and log were read for a failed write; the results are asserted in `tests/core/test_compose_pooler.py`.
- **Gaps:** nothing ran on the droplet; the flame-graph panel was not opened in a browser; profiler overhead was not measured; the session-state scan reads SQL strings and cannot see SQL assembled from pieces.
- **Use when:** a connection, retry or cache setting uses `0`, `False` or `None` and the library's documentation has not been read for which one means "disabled"; or a monitoring tool shows nothing and the first suspect is the client.

### 100. A cache and a queue cannot share an eviction policy, and turning persistence on is a migration
Production had one Redis for the work queue, locks, cancel flags, dedup claims, the front door's rate buckets and revocation list, Telegram state **and** the semantic cache (R15).
- **No policy serves both:** a cache wants `allkeys-lru`, the queue and locks must never be evicted, and `volatile-lru` would evict exactly the keys that carry an expiry (locks, results streams, claims). The queue's Redis now has `noeviction` and a `maxmemory`, and the cache has its own Redis. A full one REFUSES the write (the visible failure you want), so an alert at 80% is part of the change.
- **Without a `maxmemory`, a limit is a kill:** the container had a `mem_limit` and Redis none, so it grew until the kernel killed it.
- **Switching persistence on for an existing volume starts it empty:** with `--appendonly yes` and only a `dump.rdb`, Redis ignores the snapshot, which here would have **un-revoked people**. The safe order: `CONFIG SET appendonly yes` in the RUNNING server, wait for the rewrite, then start the new service on the same volume.
- **A plain Redis cannot load a Stack server's AOF** (the cache's search and JSON commands are in it), so both services stay on the Stack image until both can move to Redis 8 together. The Stack image takes its flags from `REDIS_ARGS`, not `command:`.
- **An alert on a cache's memory is noise,** so the 80% rule watches the queue only.
- **Gaps:** nothing ran on a droplet; `maxmemory` values are defaults for the owner to confirm; no `requirepass`.
- **Use when:** one datastore serves a cache and state that must not be lost; or a durability flag is changed on a store that already holds data.

### 101. A backup is proved by a restore into a server that has never seen the data, and the first one finds what the backup could not
R26 asked for a backup job and "one restore that was actually performed". Making both real found four things that reading the files had not.
- **A new production volume could not start:** the compose file set `POSTGRES_DB: appdata`, so the image created `appdata` and `02-appdata.sql` then failed on `CREATE DATABASE`. Nothing had ever started production's Postgres on an empty volume, since test helpers start the dev layout. The drill and a regression test start the production service as compose defines it.
- **A script's only outside world is a command, so run the real script against a fake of it:** a stand-in `docker` on `PATH` ran the real `backup.sh` and found GNU-only `head -n -N` in the prune within minutes. A backup is complete only when its directory has lost `.partial`, and any failing step fails the run.
- **A snapshot does not carry the alias the app uses:** Qdrant snapshots are per physical collection, so the `-live` aliases are written beside them and put back, and the test restores into a SECOND server and searches through the alias.
- **Verify the copy off the box:** a bucket copy is complete only when its manifest (sizes, SHA-256) exists, and the download refuses a mismatch. The check that the job still runs (`--latest`) belongs off the droplet.
- **Gaps:** numbers are a laptop's on small data; end-to-end recovery time is unmeasured; nothing alerts that a backup stopped.
- **Use when:** a backup or migration is written but no restore of it has been performed; or a test helper starts a service in a layout that differs from production's.

### 102. A pool's own "check on checkout" can turn a restart into a 30-second stall; replace the dead connections together instead
After PgBouncer or Postgres restarts, every pooled connection is dead and `psycopg_pool` finds out one caller at a time (a pool of 18: up to 18 failed requests). R25 proposed `check=AsyncConnectionPool.check_connection`, to be decided by measurement.
- **Reproduced:** warm a pool, `pg_terminate_backend` every backend of the role, query one after another: the plain pool failed 6 of 10 (pool of 6) and 11 of 22 (pool of 18).
- **`check=` made it worse:** the first request failed with `PoolTimeout` after 30.0 s. The check loop discards a dead connection and then SLEEPS a doubling backoff (1, 2, 4, 8, 16 s) before the next, while replacements were added within a second. Read the library's loop before enabling a health check whose cost you only estimated.
- **What works:** the pool's own `check()`, run once in the background when a connection a caller used turns out to be `broken`, replaces every dead idle connection in one pass: 2 failures at any pool size, none slow. `SelfHealingPool` does that for both pools, never for a deadlock or a cancelled statement, at most one check at a time, re-raising exactly what the caller would have seen.
- **The test carries its own control** (88): the same scenario on a stock pool must fail.
- **Gaps:** the restart is simulated by terminating backends; a real PgBouncer restart was not exercised; `agent_db_pool_connection_lost_total{pool}` has no alert, since a burst is a restart.
- **Use when:** a pool, client or cache offers a health check "on borrow" and the failure it is meant to fix has not been reproduced with every connection dead at once.
