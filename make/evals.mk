# Evals of the model, the prompts and the retrieval, and the red team. Included by the Makefile.

##@ Model and retrieval evals

eval:  ## Golden-dataset evaluation against the real stack (needs `make up` + `make ingest`)
	python -m scripts.checks.eval

# The deadline is longer than a turn's (ROUTER_TIMEOUT_SECONDS): this judges where a question goes, not how fast.
route-suggestions:  ## Where the router sends each "Try asking" question, against its mark in the web client (needs the ML service)
	ROUTER_TIMEOUT_SECONDS=5 python -m scripts.checks.route_suggestions

# [VIA=gateway|service|inprocess] [DATASET=<name>] [OUTCOMES=<file>] (specs/014)
model-eval:  ## Evaluate ONE model against its recorded baseline: KIND=embedding|reranker|screen|classifier CANDIDATE=<name> [VIA=...]
	@test -n "$(KIND)" -a -n "$(CANDIDATE)" || { echo "usage: make model-eval KIND=embedding CANDIDATE=<name> [VIA=...] [DATASET=...]"; exit 2; }
	python -m scripts.model_eval run --kind $(KIND) --candidate $(CANDIDATE) $(if $(VIA),--via $(VIA)) $(if $(DATASET),--dataset $(DATASET)) $(if $(OUTCOMES),--outcomes $(OUTCOMES))

model-eval-all:  ## The same candidate for every kind that declares it: make model-eval-all CANDIDATE=<name>
	@test -n "$(CANDIDATE)" || { echo "usage: make model-eval-all CANDIDATE=<name>"; exit 2; }
	python -m scripts.model_eval run --all-kinds --candidate $(CANDIDATE) $(if $(VIA),--via $(VIA))

model-eval-baseline:  ## RECORD an immutable baseline (FORCE=1 replaces, ADD_HARDWARE=1 adds this machine's latency)
	@test -n "$(KIND)" -a -n "$(CANDIDATE)" || { echo "usage: make model-eval-baseline KIND=embedding CANDIDATE=<name>"; exit 2; }
	python -m scripts.model_eval baseline --kind $(KIND) --candidate $(CANDIDATE) $(if $(VIA),--via $(VIA)) $(if $(DATASET),--dataset $(DATASET)) $(if $(FORCE),--force) $(if $(ADD_HARDWARE),--add-hardware)

model-eval-data:  ## Download and verify the pinned public evaluation datasets (needs the network once)
	python -m scripts.model_eval fetch

model-eval-verify:  ## Check every committed dataset, baseline, licence and control; no model or network
	python -m scripts.model_eval verify

# [LABELSET=route-topics] [KEEP=0.95]; prints the `calibration` line (specs/015).
labelset-calibrate:  ## Choose a label set's "none of these" cutoff for ONE model on the dev split: CANDIDATE=labelset-<model>
	@test -n "$(CANDIDATE)" || { echo "usage: make labelset-calibrate CANDIDATE=<a classifier candidate with source.model, e.g. labelset-bge-small-en-v1.5>"; exit 2; }
	python -m scripts.model_eval labelset-calibrate --candidate $(CANDIDATE) $(if $(LABELSET),--labelset $(LABELSET)) $(if $(KEEP),--keep $(KEEP))

# Needs the gateway: make router-label JUDGE=<alias> [LOCAL=chat] [STRONG=chat-strong] [DEADLINE=<s>] [LIMIT=<n>]; resumable.
router-label:  ## Score the route prompts' answers from the local and strong model with a judge (slow): JUDGE=<alias>
	@test -n "$(JUDGE)" || { echo "usage: make router-label JUDGE=<gateway alias that scores the answers>"; exit 2; }
	python -m scripts.model_eval route-label --judge $(JUDGE) $(if $(LOCAL),--local $(LOCAL)) $(if $(STRONG),--strong $(STRONG)) $(if $(STRONG_ID),--strong-id $(STRONG_ID)) $(if $(DEADLINE),--deadline $(DEADLINE)) $(if $(LIMIT),--limit $(LIMIT))

# ARGS=--no-loops runs the quick half. Run after ANY change to a chat alias's model or repeat_penalty (GRAPH_PATTERNS.md 73).
# Run it before and after any change to a prompt, a tool description or app/core/prompt_rules.py: ARGS="--out before.json", then
# ARGS="--compare before.json". Native Ollama only, nothing else started.
prompt-check:  ## Does each domain's prompt still make the chat model do the right thing first? (~5 min)
	python -m scripts.checks.prompt_behaviour $(ARGS)

probe-tools:  ## Does the chat model emit usable tool calls? 25 prompts plus 4 that must not loop (~2 min); rerun after any chat alias change
	python -m scripts.checks.toolcall_probe $(ARGS)

##@ Prompt checks and LLM-judged quality

# `npm install`, not `npm ci`: the lock's optional native bindings (libsql, one per OS and arch) are a snapshot of the machine that wrote it,
# and `npm ci` then fails on Linux with "libsql binding is missing". `--include=optional` is promptfoo's documented fix (needed either way).
promptfoo:  ## Prompt regression checks for the domain system prompts against a real Ollama
	cd promptfoo && npm install --include=optional
	python -m promptfoo.dump_prompts
	cd promptfoo && for domain in support ops sales; do \
		npx promptfoo eval --config $$domain.yaml || exit 1; \
	done

# Read the reasons, not just pass/fail (GRAPH_PATTERNS.md 48); it never fails on a score. Nightly in .github/workflows/llm-evals.yml.
# `tests/deepeval`, not the whole suite: collecting the API tests configures logging at import and floods every graph turn with it.
deepeval:  ## LLM-judged RAG quality, conversation simulation and tool-call checks (needs Docker, GOOGLE_API_KEY, GROQ_API_KEY)
	DEEPEVAL_TELEMETRY_OPT_OUT=1 pytest -m deepeval -q -s -v --reruns 2 --reruns-delay 10 --only-rerun "(ServerError|UNAVAILABLE|RESOURCE_EXHAUSTED|DEADLINE_EXCEEDED|RateLimitError|APITimeoutError|APIConnectionError|InternalServerError)" tests/deepeval

##@ Red team

# Target stays local; see promptfoo/redteam.yaml.
# Every promptfoo command runs from promptfoo/ (package.json lives there), so --config and --output are relative to it.
# --max-concurrency 1 --delay 2100: serial calls >2s apart, under the free Gemini tier's 30 req/min (redteam.yaml, RATE LIMIT).
# -o per stanza: `redteam run` writes to promptfoo/redteam.yaml whatever --config is (promptfoo's own default), which clobbered
# the hand-authored support file once; so each scratch config is its own output. Explicit stanzas, not a loop: support's file has no domain prefix.
# promptfoo exits 100 when a case fails. A finding is the point of a red team, so 100 carries on to the next domain; any other failure
# (npm, Ollama, the Gemini key, a crash) stops here and fails the run.
redteam_run = cp promptfoo/$(1) promptfoo/.$(2)-redteam-run-scratch.yaml && cd promptfoo && \
	{ PROMPTFOO_DISABLE_REMOTE_GENERATION=true npx promptfoo redteam run --config .$(2)-redteam-run-scratch.yaml \
	--output .$(2)-redteam-run-scratch.yaml --max-concurrency 1 --delay 2100 || test $$? -eq 100; }

promptfoo-redteam:  ## Adversarial variants of the support/sales/ops prompts, graded by Gemini (needs GOOGLE_API_KEY)
	cd promptfoo && npm install --include=optional
	python -m promptfoo.dump_prompts
	$(call redteam_run,redteam.yaml,support)
	$(call redteam_run,sales-redteam.yaml,sales)
	$(call redteam_run,ops-redteam.yaml,ops)

garak:  ## Fast garak jailbreak/injection probes; run in a SEPARATE Python env (garak upgrades langgraph-checkpoint past our pin)
	python -m pip install -q -r garak/requirements-garak.txt
	python garak/run_ci_scan.py

garak-full:  ## The full, slow garak suite: a pre-release scan, not a per-PR gate; same separate env
	python -m pip install -q -r garak/requirements-garak.txt
	OPENAICOMPATIBLE_API_KEY="sk-not-checked-by-ollama" python -m garak --config garak/config.yaml --target_type openai.OpenAICompatible --target_name "$${GARAK_MODEL:-qwen2.5:3b}"
