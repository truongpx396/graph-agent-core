"""`scripts/tools/compose_check.py` (`make compose-check`, CI's `config` job): here, that it checks every compose file there is. It runs Docker itself in CI."""
from pathlib import Path

from scripts.tools import compose_check
from tests.compose_project import parts

REPO = Path(__file__).resolve().parents[3]


def test_every_compose_file_is_checked_by_some_project():
    checked = {part for _, files in compose_check.PROJECTS for file in files for part in parts(REPO / file)}
    on_disk = set((REPO / "deploy").glob("*/*.yml"))
    assert not on_disk - checked, f"never validated: {sorted(str(path.relative_to(REPO)) for path in on_disk - checked)}"


def test_every_env_example_it_names_exists_and_required_variables_are_found():
    assert all((REPO / env_file).is_file() for env_file, _ in compose_check.PROJECTS)
    assert compose_check.required_variables(["a: ${ONE:?set it}\nb: ${TWO?}\nc: ${THREE:-default}\nd: ${FOUR}"]) == {"ONE", "TWO"}
