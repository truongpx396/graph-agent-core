"""`docker compose config` on every compose project and overlay this repository ships (`make compose-check`, CI's `config` job). Needs Docker's CLI only:
nothing is pulled or started. A file an entry includes, a bad anchor or a dangling `depends_on` is otherwise found on the machine that runs `up`.

Variables a machine must set by hand (`${NAME:?...}`) get a placeholder; everything else comes from that environment's committed example file.
"""
import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
REQUIRED = re.compile(r"\$\{([A-Z_0-9]+):?\?")
# (example env file, compose files in the order compose merges them)
PROJECTS = (
    (".env.example", ("deploy/dev/docker-compose.yml",)),
    (".env.example", ("deploy/dev/docker-compose.yml", "deploy/dev/collectors.yml")),
    (".env.example", ("deploy/dev/docker-compose.yml", "deploy/dev/hosted-llm.yml")),
    (".env.example", ("deploy/dev/docker-compose.yml", "deploy/dev/loadtest.yml")),
    (".env.example", ("deploy/dev/observability.yml",)),
    ("deploy/prod/prod.env.example", ("deploy/prod/docker-compose.yml",)),
    ("deploy/prod/prod.env.example", ("deploy/prod/docker-compose.yml", "deploy/prod/tools.yml")),
    ("deploy/prod/observability.env.example", ("deploy/prod/observability.yml",)),
)


def required_variables(texts: list[str]) -> set[str]:
    return {name for text in texts for name in REQUIRED.findall(text)}


def main() -> int:
    every = [path.read_text(encoding="utf-8") for path in (REPO / "deploy").rglob("*.yml")]
    env = {**dict.fromkeys(required_variables(every), "placeholder"), **os.environ}
    failed = 0
    for env_file, files in PROJECTS:
        flags = [flag for file in files for flag in ("-f", file)]
        result = subprocess.run(
            # Every profile: without this, a service behind one (the gateway, the sandbox, SonarQube) is not checked at all.
            ["docker", "compose", "--project-directory", ".", "--env-file", env_file, *flags, "--profile", "*", "config", "--quiet"],
            cwd=REPO, env=env, capture_output=True, text=True, check=False,
        )
        print(f"{'ok  ' if result.returncode == 0 else 'FAIL'} {' + '.join(files)}")
        if result.returncode:
            print(result.stderr.strip(), file=sys.stderr)
            failed += 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
