"""The test runner is a dev dependency, pinned in ONE place: `requirements-test.txt`.

`pytest` and `pytest-asyncio` sat in `requirements.txt`, so the lock file carried them and the production image shipped a test runner (and its
`pluggy`, `iniconfig`) it never runs. Then each CI job that runs tests named the pins again on its own install line, five copies kept equal by
a test. Now the CI jobs and `requirements-dev.txt` read the same file; these checks keep it that way.
"""
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / ".github" / "workflows"
# Installs only `./packages/billing[test]`, whose own pyproject pins its test tools (the job exists to prove the package needs nothing else).
OWN_TEST_EXTRA = {"ci.yml:billing-package"}
# A step that runs the Python tests: `pytest ...`, or a make target that does.
RUNS_PYTHON_TESTS = re.compile(r"(?m)^\s*(?:pytest\s|make (?:test(?:-[a-z0-9-]+)?|deepeval)\b)")
# What the hermetic suite imports that the lock file does not carry, and one more: the ML service image installs prometheus-client itself.
SHARED_PINS = {"services/ml-service/requirements.txt": ("prometheus-client",), "requirements-deepeval.txt": ("ollama", "google-auth")}


def _requirement_names(filename: str) -> set[str]:
    names = set()
    for line in (ROOT / filename).read_text(encoding="utf-8").splitlines():
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if match and not line.startswith("#"):
            names.add(re.sub(r"[-_.]+", "-", match.group(1)).lower())
    return names


def _pin(filename: str, package: str) -> str:
    found = re.search(rf"(?m)^{re.escape(package)}==(\S+)$", (ROOT / filename).read_text(encoding="utf-8"))
    assert found, f"{filename} must pin {package} exactly"
    return found.group(1)


def test_the_runtime_requirements_and_the_lock_file_name_no_pytest_package():
    for filename in ("requirements.txt", "requirements-lock.txt"):
        found = sorted(n for n in _requirement_names(filename) if n == "pytest" or n.startswith("pytest-"))
        assert not found, f"{filename} is what the app image installs; a test tool belongs in requirements-test.txt: {found}"


def test_every_job_that_runs_the_tests_installs_requirements_test():
    checked = []
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for name, job in yaml.safe_load(path.read_text(encoding="utf-8"))["jobs"].items():
            steps = job.get("steps", [])
            job_id = f"{path.name}:{name}"
            if job_id in OWN_TEST_EXTRA or not any(RUNS_PYTHON_TESTS.search(step.get("run", "")) for step in steps):
                continue
            checked.append(job_id)
            installed = " ".join(step.get("with", {}).get("requirements", "") for step in steps)
            assert "requirements-test.txt" in installed.split(), f"{job_id} runs the tests but does not install requirements-test.txt"
    assert {"ci.yml:test", "ci.yml:test-integration", "ci.yml:test-live", "llm-evals.yml:deepeval"} <= set(checked), f"test jobs found: {checked}"


def test_the_dev_requirements_include_the_files_ci_installs_and_pin_nothing_of_theirs_again():
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
    for filename in ("requirements-test.txt", "requirements-lint.txt", "requirements-deepeval-nodeps.txt"):
        assert f"\n-r {filename}\n" in dev, f"requirements-dev.txt must include {filename}"
        again = _requirement_names(filename) & _requirement_names("requirements-dev.txt")
        assert not again, f"pinned twice, in requirements-dev.txt and {filename}: {sorted(again)}"


def test_a_package_pinned_in_two_files_has_one_version():
    for other, packages in SHARED_PINS.items():
        base = "requirements-test.txt" if "prometheus-client" in packages else "requirements-dev.txt"
        for package in packages:
            assert _pin(other, package) == _pin(base, package), f"{package} differs between {base} and {other}"
