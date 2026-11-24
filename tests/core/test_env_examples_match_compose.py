"""Each environment's `.env` example lists every variable its compose files read, and no variable they do not.

`.env.example` is held to `Settings` by test_env_example_matches_settings.py. The production examples were held to nothing, and drifted both ways: the compose files read
`ML_MODELS_ENABLED`, `ML_EMBED_MAX_ITEMS`, `ML_EMBED_MAX_CHARS` and `WEB_SEARCH_MAX_RESULTS` which the example never mentioned (a knob nobody could find), and the example
told operators to set `MAX_COST_USD_PER_TENANT_PER_MONTH`, the two per-principal ceilings and `UNPRICED_MODEL_POLICY` which no compose file passed to a container, so
a $400 monthly cap in a droplet's `.env` capped nothing and `block` blocked nothing.

A container sees only what its `environment:` hands it, so for production "is in `.env`" and "is read by a compose file" must mean the same thing.
"""
import re

import pytest

from tests.compose_project import REPO, load, parts, strings

DEV_DIR, PROD_DIR = REPO / "deploy" / "dev", REPO / "deploy" / "prod"
_VARIABLE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")
_ENTRY = re.compile(r"^\s*#?\s*([A-Z][A-Z0-9_]+)\s*=", re.MULTILINE)  # a commented line counts: it is how an optional setting shows its default

# environment -> (its example, the compose files whose variables it must list)
ENVIRONMENTS = {
    "dev": (REPO / ".env.example", sorted(path for path in DEV_DIR.glob("*.yml") if path.name != "hosted-llm.yml")),
    # The dev droplet has a .env of its own (its LLM is hosted, and a laptop's Strix credentials share these names, so the two must not be one file).
    "dev-droplet": (DEV_DIR / "droplet.env.example", [DEV_DIR / "hosted-llm.yml"]),
    # tools.yml is an overlay, not an included part, but a droplet that enables it reads its variables from the same .env.
    "prod": (PROD_DIR / "prod.env.example", [*parts(PROD_DIR / "docker-compose.yml"), PROD_DIR / "tools.yml"]),
    "prod-observability": (PROD_DIR / "observability.env.example", [PROD_DIR / "observability.yml"]),
}
# Read by a compose file but set by something else, so an operator never writes them in `.env`.
SET_ELSEWHERE = {
    "dev": {
        "AUTHLIMIT_UID", "AUTHLIMIT_GID", "AUTHLIMIT_KEYS_DIR", "CASDOOR_INIT_DIR",  # the Makefile's GATEWAY_ENV: the host user and where `make authlimit-keys` made the keys, and where `make front-door-bootstrap` writes Casdoor's init file
        "AUTHLIMIT_UPSTREAM",  # chosen per make target (the echo stub, or the API behind the door)
        "TELEGRAM_UPSTREAM", "TELEGRAM_CA_FILE",  # `make telegram-up`: Caddy's upstream for /telegram/*, and the root certificate it exports for the bot to trust
        "CASDOOR_DB_PASSWORD", "CASDOOR_ADMIN_PASSWORD",  # generated once by `make front-door-up` into a second --env-file outside the repo
        "HOME",  # the shell's
    },
    "prod": {"IMAGE_TAG"},  # passed by .github/workflows/deploy.yml at deploy time (prod.env.example says so)
    "prod-observability": set(),
    "dev-droplet": set(),
}
# Listed in an example and read by a script rather than a compose file, so the "reaches no process" test must not count them as dead.
READ_BY_SCRIPTS = {"prod": {"CRAWLER_ENABLED"}}  # deploy/prod/compose.sh decides from it whether to add the crawler overlay



def _read_by_compose(files) -> set[str]:
    return {name for path in files for text in strings(load(path)) for name in _VARIABLE.findall(text)}


def _listed(example) -> set[str]:
    return set(_ENTRY.findall(example.read_text(encoding="utf-8")))


@pytest.mark.parametrize("environment", ENVIRONMENTS)
def test_every_variable_the_compose_files_read_is_in_that_environments_example(environment):
    example, files = ENVIRONMENTS[environment]
    missing = sorted(_read_by_compose(files) - _listed(example) - SET_ELSEWHERE[environment])

    assert not missing, f"{example.relative_to(REPO)} does not list variables its compose files read (a commented-out line is fine): {', '.join(missing)}"


@pytest.mark.parametrize("environment", ["prod", "prod-observability", "dev-droplet"])
def test_a_droplet_example_entry_no_compose_file_reads_reaches_no_process(environment):
    example, files = ENVIRONMENTS[environment]
    dead = sorted(_listed(example) - _read_by_compose(files) - READ_BY_SCRIPTS.get(environment, set()))

    assert not dead, (
        f"{example.relative_to(REPO)} lists settings no compose file passes to a container, so setting them in the droplet's .env changes nothing: {', '.join(dead)}"
    )
