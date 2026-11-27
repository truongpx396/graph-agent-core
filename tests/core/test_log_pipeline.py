"""What has to agree outside the code for a log line to be found and to be safe: which containers Promtail ships, how the dashboard
filters them, and what the front door writes (RUNTIME_PATTERNS.md 112)."""
import json
import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
PROMTAIL = {"dev": "observability/promtail/promtail-config.yaml", "prod": "observability/promtail/promtail-config.prod.yaml"}
APP_SERVICES = ["api", "agent-worker", "ingest-worker", "ml-service", "mcp-gateway", "telegram-bot", "authlimit", "casdoor"]
CADDY = {"dev": "caddy-gateway", "prod": "caddy"}


def _text(path: str) -> str:
    return (REPO / path).read_text(encoding="utf-8")


@pytest.mark.parametrize("env", sorted(PROMTAIL))
def test_promtail_ships_the_app_and_its_front_door(env):
    scrape = yaml.safe_load(_text(PROMTAIL[env]))["scrape_configs"][0]
    keep = next(rule["regex"] for rule in scrape["relabel_configs"] if rule.get("action") == "keep")
    missing = [name for name in [*APP_SERVICES, CADDY[env]] if not re.fullmatch(keep, name)]
    assert not missing, f"{env}: these services' logs never reach Loki: {missing}"


def test_both_environments_run_the_same_pipeline():
    stages = [yaml.safe_load(_text(path))["scrape_configs"][0]["pipeline_stages"] for path in PROMTAIL.values()]
    assert stages[0] == stages[1]
    assert any("template" in stage for stage in stages[0]), "levels must be normalised: the dashboard filters on one spelling"


def _dashboard() -> dict:
    return json.loads(_text("observability/grafana/dashboards/logs.json"))


def test_the_log_dashboard_can_follow_a_request_or_a_conversation():
    dashboard = _dashboard()
    variables = {variable["name"]: variable for variable in dashboard["templating"]["list"]}
    assert {variables[name]["type"] for name in ("request_id", "thread_id", "search")} == {"textbox"}
    logs = next(panel for panel in dashboard["panels"] if panel["type"] == "logs")
    for name in ("request_id", "thread_id", "search", "service", "level"):
        assert f"${name}" in logs["targets"][0]["expr"]


def test_the_log_dashboard_hides_no_level_by_default():
    """It defaulted to `error|warn`: the ids were on the info lines, and Python writes `warning`, which `warn` does not match."""
    level = next(variable for variable in _dashboard()["templating"]["list"] if variable["name"] == "level")
    assert level["current"]["value"] == "$__all"
    assert level["allValue"] == ".*", "`.+` also drops every line that has no level"
    for panel in _dashboard()["panels"]:
        for target in panel.get("targets", []):
            assert not re.search(r"\bwarn\b", target["expr"]), panel["title"]


def test_the_id_in_a_line_links_to_the_dashboard_that_shows_its_request():
    datasources = yaml.safe_load(_text("observability/grafana/provisioning/datasources/datasources.yml"))["datasources"]
    fields = next(source for source in datasources if source["uid"] == "loki")["jsonData"]["derivedFields"]
    link = next(field for field in fields if field["name"] == "request_id")
    assert re.search(link["matcherRegex"], '{"request_id": "abc123", "message": "x"}').group(1) == "abc123"
    assert link["url"].startswith(f"/d/{_dashboard()['uid']}?var-request_id=")


def test_the_front_door_logs_no_query_string_and_no_header():
    """Caddy's default wrote a sign-in's `code` and the Telegram webhook's secret header."""
    shared = _text("deploy/shared/caddy/redacted-format.caddyfile")
    for rule in (r"request>uri regexp \?.* ?REDACTED", "request>headers delete", "resp_headers delete"):
        assert rule in shared


@pytest.mark.parametrize("entry", ["deploy/dev/caddy/Caddyfile", "deploy/prod/caddy/Caddyfile"])
def test_no_log_of_either_front_door_skips_the_redaction(entry):
    routing = _text("deploy/shared/caddy/gateway.caddyfile")
    for site in ("api_site", "auth_site", "mcp_site"):
        assert f"({site}) {{\n\timport access_log\n" in routing
    text = _text(entry)
    # The server's own lines (a failed upstream's request) are filtered, and a site declares no log of its own.
    assert re.search(r"log default \{\s*import \.\./\.\./shared/caddy/redacted-format\.caddyfile", text)
    assert len(re.findall(r"^\s*log\b", text, re.MULTILINE)) == 1


# --- Traces (RUNTIME_PATTERNS.md 113) ---


def _yaml(path: str) -> dict:
    return yaml.safe_load(_text(path))


def test_the_collectors_carry_spans_to_tempo():
    gateway, agent = _yaml("observability/otel-collector/config.yaml"), _yaml("observability/otel-collector/config.agent.yaml")
    assert gateway["service"]["pipelines"]["traces"] == {"receivers": ["otlp"], "processors": ["batch"], "exporters": ["otlp_http/tempo"]}
    assert agent["service"]["pipelines"]["traces"]["exporters"] == ["otlp_http/gateway"]
    listens = _yaml("observability/tempo/tempo.yaml")["distributor"]["receivers"]["otlp"]["protocols"]["http"]["endpoint"]
    assert gateway["exporters"]["otlp_http/tempo"]["endpoint"] == f"http://tempo:{listens.rsplit(':', 1)[1]}"


@pytest.mark.parametrize("env", ["dev", "prod"])
def test_each_observability_project_runs_tempo_unpublished(env):
    tempo = _yaml(f"deploy/{env}/observability.yml")["services"]["tempo"]
    assert "ports" not in tempo, "only the collector writes to it and only Grafana reads it"
    assert "./observability/tempo/tempo.yaml:/etc/tempo/tempo.yaml:ro" in tempo["volumes"]


def test_a_trace_is_kept_as_long_as_the_lines_that_name_it():
    tempo = _yaml("observability/tempo/tempo.yaml")["backend_worker"]["compaction"]["block_retention"]
    assert tempo == _yaml("observability/loki/loki-config.yaml")["limits_config"]["retention_period"]


def test_a_line_opens_its_trace_and_a_trace_its_lines():
    sources = {s["uid"]: s for s in _yaml("observability/grafana/provisioning/datasources/datasources.yml")["datasources"]}
    link = next(field for field in sources["loki"]["jsonData"]["derivedFields"] if field["name"] == "trace_id")
    assert link["datasourceUid"] == "tempo"
    assert re.search(link["matcherRegex"], '{"trace_id": "' + "a" * 32 + '"}').group(1) == "a" * 32
    back = sources["tempo"]["jsonData"]["tracesToLogsV2"]
    assert back["datasourceUid"] == "loki"
    assert "__trace.traceId" in back["query"]


def test_the_front_door_records_the_trace_the_api_started():
    assert "log_append trace_id {http.response.header.X-Trace-Id}" in _text("deploy/shared/caddy/gateway.caddyfile")
