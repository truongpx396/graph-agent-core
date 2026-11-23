"""`deploy/prod/crawler-firewall.sh` (PRODUCTION_REVIEW.md R20), run for real against a stateful fake `iptables`.

The script's whole outside world is `iptables`, `docker exec`, `sudo` and `systemctl`, so stand-ins that keep state and log every call run the real bash: what is checked is
its own logic, which is where a security control goes wrong quietly. It proves: `apply` leaves exactly one jump and one drop per range however often it runs; `status` is
green only when every rule is there and says what is missing; `remove` takes everything away; the subnet follows the environment; `check` fails unless EVERY blocked range
counted a dropped packet (a timeout alone cannot tell "dropped" from "nothing there"); and `install` writes a unit that applies the rules after Docker.

It does not prove the rules block traffic: that needs a kernel. The same script was run on a real Docker daemon (a container on the subnet reached a service on the host before
`apply`, and could not after, with each range's counter moving), and deploy/README.md says to run `check` on the droplet before relying on it."""
import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "deploy" / "prod" / "crawler-firewall.sh"
SUBNET = "172.30.50.0/24"
RANGES = ["169.254.0.0/16", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10"]

FAKE_IPTABLES = r'''#!/usr/bin/env python3
"""A stateful stand-in for iptables: chains of rule strings in a JSON file. Supports exactly the verbs the script uses."""
import json, os, sys
path = os.environ["FAKE_STATE"]
state = json.load(open(path)) if os.path.exists(path) else {"INPUT": [], "DOCKER-USER": []}
argv = sys.argv[1:]
op = argv[0]

def save():
    json.dump(state, open(path, "w"))

if op == "-N":
    if argv[1] in state:
        sys.exit(1)
    state[argv[1]] = []
elif op == "-F":
    if argv[1] not in state:
        sys.exit(1)
    state[argv[1]] = []
elif op == "-X":
    if argv[1] not in state:
        sys.exit(1)
    del state[argv[1]]
elif op == "-A":
    state[argv[1]].append(" ".join(argv[2:]))
elif op == "-I":
    rest = argv[2:]
    index = int(rest.pop(0)) - 1 if rest and rest[0].isdigit() else 0
    state[argv[1]].insert(index, " ".join(rest))
elif op in ("-C", "-D"):
    rule = " ".join(argv[2:])
    chain = state.get(argv[1])
    if chain is None or rule not in chain:
        sys.exit(1)
    if op == "-D":
        chain.remove(rule)
elif op == "-Z":
    pass
elif op == "-nvx":                       # -nvx -L CHAIN: pkts bytes target prot opt in out source destination
    pkts = json.loads(os.environ.get("FAKE_PKTS", "{}"))
    print("Chain %s (1 references)" % argv[2])
    print("    pkts      bytes target     prot opt in     out     source               destination")
    for rule in state.get(argv[2], []):
        words = rule.split()
        dest = words[words.index("-d") + 1] if "-d" in words else "0.0.0.0/0"
        print("%8d %10d %-10s 0    --  *      *       0.0.0.0/0            %s" % (pkts.get(dest, 0), 0, words[words.index("-j") + 1], dest))
else:
    sys.exit("fake iptables: unsupported " + " ".join(argv))
save()
'''
FAKE_SUDO = '#!/bin/sh\n[ "$1" = "-n" ] && shift\nexec "$@"\n'
FAKE_DOCKER = '#!/bin/sh\necho "docker $*" >> "$FAKE_LOG"\n'
FAKE_SYSTEMCTL = '#!/bin/sh\necho "systemctl $*" >> "$FAKE_LOG"\n'


@pytest.fixture
def env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("iptables", FAKE_IPTABLES), ("sudo", FAKE_SUDO), ("docker", FAKE_DOCKER), ("systemctl", FAKE_SYSTEMCTL)):
        (bin_dir / name).write_text(body, encoding="utf-8")
        (bin_dir / name).chmod((bin_dir / name).stat().st_mode | stat.S_IXUSR)
    return {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "FAKE_STATE": str(tmp_path / "state.json"),
        "FAKE_LOG": str(tmp_path / "log.txt"),
        "SYSTEMD_UNIT_DIR": str(tmp_path),
        "CRAWLER_CONTAINER": "crawl4ai-1",
    }


def run(env, *args, **extra):
    return subprocess.run(["bash", str(SCRIPT), *args], env={**env, **extra}, capture_output=True, text=True)


def state(env):
    return json.loads(Path(env["FAKE_STATE"]).read_text(encoding="utf-8"))


def test_apply_leaves_one_jump_and_one_drop_per_range_however_often_it_runs(env):
    for _ in range(3):
        assert run(env, "apply").returncode == 0
    rules = state(env)
    assert rules["DOCKER-USER"] == [f"-s {SUBNET} -j CRAWLER-EGRESS"]
    assert rules["INPUT"] == [f"-s {SUBNET} -j CRAWLER-INPUT"]
    egress = rules["CRAWLER-EGRESS"]
    assert egress[0] == f"-d {SUBNET} -j RETURN", "its own network is reached before any drop"
    assert [r for r in egress if r.endswith("-j DROP")] == [f"-d {r} -j DROP" for r in RANGES]
    assert egress[-1] == "-j RETURN", "everything else is the internet"
    assert rules["CRAWLER-INPUT"][-1] == "-j DROP", "the host's own services are not the crawler's"


def test_status_is_green_after_apply_and_names_what_is_missing_before_it(env):
    before = run(env, "status")
    assert before.returncode == 1
    assert "missing: DOCKER-USER jump" in before.stderr and "not installed" in before.stderr

    run(env, "apply")
    assert run(env, "status").returncode == 0

    # a single rule deleted by hand turns it red and says which one
    data = state(env)
    data["CRAWLER-EGRESS"].remove("-d 169.254.0.0/16 -j DROP")
    Path(env["FAKE_STATE"]).write_text(json.dumps(data), encoding="utf-8")
    after = run(env, "status")
    assert after.returncode == 1 and "missing: drop for 169.254.0.0/16" in after.stderr


def test_remove_takes_every_rule_and_chain_away_and_status_goes_red(env):
    run(env, "apply")
    assert run(env, "remove").returncode == 0
    assert state(env) == {"INPUT": [], "DOCKER-USER": []}
    assert run(env, "status").returncode == 1


def test_the_subnet_comes_from_the_environment_so_it_can_follow_tools_yml(env):
    run(env, "apply", CRAWLER_SUBNET="172.31.7.0/24")
    assert state(env)["DOCKER-USER"] == ["-s 172.31.7.0/24 -j CRAWLER-EGRESS"]
    assert run(env, "status").returncode == 1, "the default subnet's rules are not there"
    assert run(env, "status", CRAWLER_SUBNET="172.31.7.0/24").returncode == 0


def test_check_passes_only_when_every_blocked_range_counted_a_dropped_packet(env):
    run(env, "apply")
    all_counted = json.dumps(dict.fromkeys(RANGES, 3))
    ok = run(env, "check", FAKE_PKTS=all_counted)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert all(f"dropped  {r}" in ok.stdout for r in RANGES) and "internet reachable" in ok.stdout

    one_silent = json.dumps({r: 3 for r in RANGES if r != "10.0.0.0/8"})
    bad = run(env, "check", FAKE_PKTS=one_silent)
    assert bad.returncode == 1 and "NOT DROPPED  10.0.0.0/8" in bad.stdout, "a probe that no rule counted is a rule that is not working"

    probes = [line for line in Path(env["FAKE_LOG"]).read_text(encoding="utf-8").splitlines() if "169.254.169.254" in line]
    assert probes and all(line.startswith("docker exec crawl4ai-1 curl") for line in probes), "the probes run inside the crawler, not on the host"


def test_check_refuses_to_run_without_the_rules(env):
    assert run(env, "check").returncode == 1


def test_install_writes_a_unit_that_applies_the_rules_after_docker_and_enables_it(env):
    result = run(env, "install")
    assert result.returncode == 0, result.stderr
    unit = (Path(env["SYSTEMD_UNIT_DIR"]) / "crawler-firewall.service").read_text(encoding="utf-8")
    assert "After=docker.service" in unit and "Requires=docker.service" in unit, "a Docker restart must re-apply them"
    assert f"ExecStart={SCRIPT} apply" in unit and f"ExecStop={SCRIPT} remove" in unit
    log = Path(env["FAKE_LOG"]).read_text(encoding="utf-8")
    assert "systemctl daemon-reload" in log and "systemctl enable --now crawler-firewall.service" in log


def test_an_unknown_command_is_a_usage_error(env):
    assert run(env, "frobnicate").returncode == 2
