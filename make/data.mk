# Models and the vector store: what to pull, embed and rebuild before the app has anything to answer with. Included by the Makefile.

##@ Models and the vector store

pull-models:  ## Pull the chat (qwen2.5:7b), test (qwen2.5:3b) and embedding models into the native Ollama (~7 GB)
	ollama pull qwen2.5:7b
	ollama pull qwen2.5:3b
	ollama pull nomic-embed-text

pull-strong-model:  ## Pull qwen2.5:14b, a local alternative for `chat-strong` (~9 GB; only if you point that alias back at Ollama)
	ollama pull qwen2.5:14b

ingest:  ## Embed sample docs and upsert them into Qdrant
	python -m scripts.tools.seed

index-skills:  ## Embed the skills catalog (name+description) and upsert into its own Qdrant collection
	python -m scripts.tools.index_skills

qdrant-indexes:  ## Create the payload indexes on collections made before they existed (once per deployment, online): [COLLECTION_NAME=docs]
	python -m scripts.tools.qdrant_indexes $(COLLECTION_NAME)

# Deliberately NOT `COLLECTION=`: that is also the Settings variable naming the main collection, and make exports command-line
# variables to the recipe, so `COLLECTION=skills` would rename the main collection for this process and reject `docs`.
reembed:  ## Rebuild a collection under the current embedding identity, then swap it in atomically: COLLECTION_NAME=docs [ARGS=--dry-run]
	@test -n "$(COLLECTION_NAME)" || { echo "usage: make reembed COLLECTION_NAME=<docs|skills collection> [ARGS=--dry-run]"; exit 2; }
	python -m scripts.tools.reembed $(COLLECTION_NAME) $(ARGS)
