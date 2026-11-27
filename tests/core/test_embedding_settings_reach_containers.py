"""The embedding identity's settings reach every container that embeds (pattern 67).

A container sees only what its `environment:` hands it. Neither compose project passed the prefixes or `EMBED_MODEL_REVISION`, and production fixed
`EMBED_MODEL`, so a value set in `.env` changed nothing there, while `make ingest` on a developer's host did read it and built a collection the
containers then refused. The generic env-example test cannot see this: it compares an example with what compose reads, not with what Settings needs.
"""
import pytest

from tests.compose_project import REPO, load

IDENTITY_SETTINGS = ("EMBED_MODEL", "EMBED_QUERY_PREFIX", "EMBED_DOCUMENT_PREFIX", "EMBED_MODEL_REVISION")
EMBEDDING_SERVICES = ("api", "agent-worker", "ingest-worker")  # what searches or writes the vector store


@pytest.mark.parametrize("environment", ["dev", "prod"])
@pytest.mark.parametrize("service", EMBEDDING_SERVICES)
def test_a_value_set_in_dotenv_reaches_the_container(environment, service):
    passed = load(REPO / "deploy" / environment / "app.yml")["services"][service]["environment"]

    for name in IDENTITY_SETTINGS:
        assert str(passed.get(name, "")).startswith(f"${{{name}:-"), f"{environment}/{service} does not take {name} from .env"
