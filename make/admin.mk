# Back-office operator commands: run once and exit (the long-running loops are in host.mk). Included by the Makefile.

##@ Sweeps and digests

ops-digest:  ## One-shot metrics digest from the ops bot to the team channel (cron it)
	python -m scripts.jobs.ops_digest

followup-sweep:  ## One-shot sweep of due sales follow-ups, drafting nudges for a human to review (cron it)
	python -m scripts.jobs.followup_sweep

tool-call-dedup-sweep:  ## Delete stale tool_call_dedup rows past their retention window (cron it)
	python -m scripts.jobs.tool_call_dedup_sweep

# Cron it once the retention policy is decided.
checkpoint-sweep:  ## Delete conversations older than CHECKPOINT_RETENTION_DAYS (0 = off): ARGS=--dry-run first
	python -m scripts.jobs.checkpoint_sweep $(ARGS)

# Counts only; ARGS="--date YYYY-MM-DD". PRODUCTION_REVIEW.md R28.
answer-sample:  ## Count answers with an invented source or a missing or wrong attribution in yesterday's conversations (advisory)
	python -m scripts.jobs.answer_sample $(ARGS)

# Sizes: ARGS="--points 50000". PRODUCTION_REVIEW.md R26.
restore-drill:  ## Restore backups of seeded Postgres, Casdoor DB, Qdrant and Redis into fresh containers and time it (needs Docker)
	python -m scripts.checks.restore_drill $(ARGS)

usage-events-sweep:  ## Delete usage events older than USAGE_EVENT_RETENTION_DAYS (min 35); cron it once retention is decided
	python -m scripts.jobs.usage_events_sweep

# Run before deploying that change and once after, so the caps keep the month so far.
usage-events-carry-over:  ## One-time, idempotent: copy older usage_ledger history into usage_events (ARGS=--dry-run, --tenant acme)
	python -m scripts.tools.usage_events_carry_over $(ARGS)

billing-usage-sweep:  ## Delete billing usage events older than BILLING_USAGE_EVENT_RETENTION_DAYS, never an unfinished export
	python -m credit_billing.cli.usage_sweep

billing-inbox-sweep:  ## Delete applied/ignored webhook inbox rows older than BILLING_INBOX_RETENTION_DAYS, never open or quarantined ones
	python -m credit_billing.cli.inbox_sweep

##@ Spend, keys, credit wallet, billing schema

budget-policy:  ## Per-tenant and per-person spend-limit overrides: ARGS="show --tenant acme --principal alice"
	python -m scripts.tools.budget_policy $(ARGS)

# Also sets the default per-person budget: ARGS="person-budget set --max-budget 20".
litellm-key:  ## Mint or inspect the app's budget-capped LiteLLM key: LITELLM_MASTER_KEY=... ARGS="create --max-budget 600"
	python -m scripts.tools.litellm_key $(ARGS)

credits:  ## Credit wallet CLI: ARGS="grant --tenant acme --amount 500 --by alice --reason '...'" (also adjust, show)
	python -m credit_billing.cli.credits $(ARGS)

billing-migrate:  ## Apply the billing schema scripts in order; safe to re-run
	python -m credit_billing.cli.migrate $(ARGS)

billing-migrate-status:  ## Show which billing schema scripts are applied, pending or changed (ARGS=--check exits 1 if any is not current)
	python -m credit_billing.cli.migrate --status $(ARGS)

##@ Reconcile checks

credit-reconcile:  ## Wallet check: was every usage in the lookback window charged? Exit 0 ok, 1 drift, 2 unfinished, 3 database down
	python -m credit_billing.cli.reconcile $(ARGS)

usage-reconcile:  ## Meter check against the LLM gateway's spend log. Exit 0 agree, 1 drift, 2 unfinished; needs LITELLM_MASTER_KEY
	python -m scripts.jobs.usage_reconcile $(ARGS)

##@ People and tokens

# Needs TELEGRAM_PERSON_SECRET and the gateway stack's Casdoor.
telegram-person:  ## Add, disable or enable a Telegram person at the authorization server: ACTION=add|disable|enable TELEGRAM_ID=<id>
	@test -n "$(TELEGRAM_ID)" || { echo "usage: make telegram-person ACTION=add|disable|enable TELEGRAM_ID=<telegram user id>"; exit 2; }
	@. $(FRONT_DOOR_SECRETS) && export CASDOOR_ADMIN_PASSWORD && python -m scripts.tools.telegram_person $(or $(ACTION),add) --telegram-id "$(TELEGRAM_ID)"

authlimit-revoke:  ## Void a person's tokens on every authlimit replica within seconds: TENANT=ecorp SUB=<person id>
	@test -n "$(TENANT)" -a -n "$(SUB)" || { echo "usage: make authlimit-revoke TENANT=<tenant> SUB=<subject>"; exit 2; }
	$(GATEWAY_COMPOSE) exec -T --index 1 authlimit /authlimit revoke --tenant "$(TENANT)" --sub "$(SUB)"
