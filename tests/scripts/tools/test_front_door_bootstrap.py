"""scripts/tools/front_door_bootstrap.py — what it asks Casdoor to create (specs/017, extended by specs/018).

Hermetic: `Casdoor` is replaced by a recorder, so these pin WHAT the script asks for (the applications, their scopes, redirect URIs and token
lifetime), not Casdoor's behaviour; that is measured against the real image by `make mcp-gateway-smoke` and `make authlimit-smoke`.

The decision under test (spec 018, FR-022): one `--token-hours` setting, default seven days, for EVERY application the script creates, MCP
ones included. Verification is offline, so the lifetime is the window a stolen token works in; revocation (`make authlimit-revoke`) is the
bound, and a test that the default is 168 is a test that nobody quietly changes it back to one hour or forward to a year.
"""
import pytest

from scripts.tools import front_door_bootstrap as bootstrap


class FakeCasdoor:
    """Records what is ensured; returns a client id derived from the application name."""

    instances: list["FakeCasdoor"] = []

    def __init__(self, base_url: str, admin_password: str) -> None:
        self.organizations: list[str] = []
        self.applications: list[dict] = []
        self.users: list[tuple[str, str]] = []
        FakeCasdoor.instances.append(self)

    def ensure_organization(self, name: str) -> None:
        self.organizations.append(name)

    def ensure_user(self, organization: str, name: str) -> str | None:
        self.users.append((organization, name))
        return "generated-password"

    def ensure_application(self, name, organization, scope, description, redirect_uris, token_hours, kind="MCP", client_id=None) -> str:
        self.applications.append(
            {
                "name": name, "organization": organization, "scope": scope, "redirect_uris": redirect_uris, "token_hours": token_hours,
                "kind": kind, "client_id": client_id,
            }
        )
        return client_id or f"client-{name}"


@pytest.fixture
def casdoor(monkeypatch):
    FakeCasdoor.instances = []
    monkeypatch.setattr(bootstrap, "Casdoor", FakeCasdoor)
    monkeypatch.setenv("CASDOOR_ADMIN_PASSWORD", "not-a-real-password")
    for var in ("TOKEN_HOURS", "AUTHLIMIT_AUDIENCE", "MCP_GATEWAY_PUBLIC_URL", "MCP_GATEWAY_OPS_TENANTS"):
        monkeypatch.delenv(var, raising=False)
    return lambda argv=(): (bootstrap.main(list(argv)), FakeCasdoor.instances[-1])[1]


def by_name(fake: FakeCasdoor) -> dict[str, dict]:
    return {a["name"]: a for a in fake.applications}


def test_every_application_lasts_seven_days_by_default(casdoor):
    fake = casdoor()

    apps = by_name(fake)
    assert set(apps) == {"mcp-ecorp", "api-ecorp", "mcp-ops"}
    assert {a["token_hours"] for a in apps.values()} == {168}, "the default is seven days for the MCP applications too (spec 018 FR-022)"
    assert bootstrap.DEFAULT_TOKEN_HOURS == 168


def test_the_token_hours_option_and_the_environment_change_it_for_every_application(casdoor, monkeypatch):
    assert {a["token_hours"] for a in casdoor(["--token-hours", "24"]).applications} == {24}

    monkeypatch.setenv("TOKEN_HOURS", "48")
    assert {a["token_hours"] for a in casdoor().applications} == {48}, "TOKEN_HOURS sets the default"
    assert {a["token_hours"] for a in casdoor(["--token-hours", "2"]).applications} == {2}, "and the option beats it"


@pytest.mark.parametrize("hours", ["0", "-1", "8761", "100000"])
def test_an_absurd_token_lifetime_is_refused_before_anything_is_created(casdoor, hours):
    with pytest.raises(SystemExit) as stop:
        casdoor(["--token-hours", hours])

    assert "between 1 and 8760" in str(stop.value)
    assert FakeCasdoor.instances == [], "nothing may be created when the arguments are wrong"


def test_the_chat_api_gets_one_public_client_per_tenant_with_its_own_scope(casdoor):
    fake = casdoor(["--tenant", "ecorp", "--tenant", "other-co"])

    apps = by_name(fake)
    assert {"api-ecorp", "api-other-co"} <= set(apps)
    api = apps["api-ecorp"]
    assert api["organization"] == "ecorp"
    assert api["scope"] == "api:chat"
    assert api["kind"] == "API"
    # The page signs in at its root URL; the smoke check and `make chat` at their own callbacks. Casdoor compares redirect URIs exactly.
    assert api["redirect_uris"] == ["https://api.localhost:8443/", bootstrap.API_SMOKE_CALLBACK, "http://localhost:33420/callback"]
    assert apps["api-other-co"]["organization"] == "other-co", "the application belongs to the tenant's organization: that is what puts `owner` in the token"


def test_the_cli_callback_port_is_the_one_the_cli_listens_on_and_can_be_moved(casdoor):
    """`make chat` listens on CHANNEL_CLI_CALLBACK_PORT and Casdoor compares redirect URIs exactly (specs/020), so the bootstrap's default must be that
    setting's default, and moving one means moving the other."""
    from app.core.config import Settings

    default = Settings.model_fields["channel_cli_callback_port"].default
    assert f"http://localhost:{default}/callback" in by_name(casdoor())["api-ecorp"]["redirect_uris"]
    assert "http://localhost:34000/callback" in by_name(casdoor(["--cli-callback-port", "34000"]))["api-ecorp"]["redirect_uris"]


def test_the_chat_apis_clients_have_the_id_the_web_page_looks_for_and_the_mcp_ones_keep_a_generated_one(casdoor):
    from app.adapters.inbound.api.web_auth import CLIENT_ID_PREFIX

    apps = by_name(casdoor(["--tenant", "ecorp", "--tenant", "other-co"]))

    for tenant in ("ecorp", "other-co"):
        assert apps[f"api-{tenant}"]["client_id"] == f"{CLIENT_ID_PREFIX}{tenant}", "the page builds this id from what the person types"
    assert apps["mcp-ecorp"]["client_id"] is None and apps["mcp-ops"]["client_id"] is None, "a pre-registered MCP client keeps its generated id"


def test_the_operators_organization_gets_no_chat_api_application(casdoor):
    # The operations team uses the MCP operations server only; a chat client for them would be an unrequested way in.
    assert not [a for a in casdoor().applications if a["organization"] == "ops-team" and a["name"].startswith("api-")]


def test_the_api_audience_follows_the_environment(casdoor, monkeypatch):
    monkeypatch.setenv("AUTHLIMIT_AUDIENCE", "https://chat.example.com/")

    api = by_name(casdoor())["api-ecorp"]

    assert api["redirect_uris"][0] == "https://chat.example.com/", "the trailing slash of the resource identifier is dropped, the page URL ends with one"


def test_the_mcp_applications_keep_their_fixed_callback_and_scopes(casdoor):
    apps = by_name(casdoor(["--callback-port", "40000"]))

    assert apps["mcp-ecorp"]["redirect_uris"] == ["http://localhost:40000/callback"]
    assert apps["mcp-ecorp"]["scope"] == "ecorp:read"
    assert apps["mcp-ops"]["scope"] == "ops:read"


def test_the_output_names_each_clients_id_and_the_lifetime(casdoor, capsys):
    casdoor(["--token-hours", "12"])

    out = capsys.readouterr().out
    assert "ecorp: client id api-ecorp" in out, "the chat API's client id is the one the web page builds from the organization"
    assert "client-mcp-ecorp" in out, "an MCP client still prints its generated id"
    assert "Access tokens last 12 hours" in out
    assert "make authlimit-revoke" in out


def test_without_the_admin_password_nothing_is_attempted(monkeypatch):
    FakeCasdoor.instances = []
    monkeypatch.setattr(bootstrap, "Casdoor", FakeCasdoor)
    monkeypatch.delenv("CASDOOR_ADMIN_PASSWORD", raising=False)

    with pytest.raises(SystemExit):
        bootstrap.main([])

    assert FakeCasdoor.instances == []


# --- the demo users' names are options, not constants ---------------------------------------------------------------------------------
# The name was `alice` and `oncall` in the code: renaming the user in Casdoor and re-running the script created the old one again, and a
# different name meant editing the script.


def test_the_demo_users_default_to_ethan_and_oncall_one_per_organization(casdoor):
    assert casdoor(["--tenant", "ecorp", "--tenant", "other-co"]).users == [("ecorp", "ethan"), ("other-co", "ethan"), ("ops-team", "oncall")]


def test_the_options_choose_the_names_and_the_old_default_is_not_created(casdoor):
    fake = casdoor(["--tenant", "ecorp", "--tenant", "other-co", "--demo-user", "priya", "--ops-user", "sre.on-call"])

    assert fake.users == [("ecorp", "priya"), ("other-co", "priya"), ("ops-team", "sre.on-call")]


def test_the_output_names_the_users_it_created(casdoor, capsys):
    casdoor(["--tenant", "ecorp", "--demo-user", "priya", "--ops-user", "sre"])

    out = capsys.readouterr().out
    assert "log in as ecorp/priya  password: generated-password" in out
    assert "log in as ops-team/sre  password: generated-password" in out
    assert "sign in as <tenant>/priya" in out, "the line that says who to sign in as at the chat page"
    assert "alice" not in out and "oncall" not in out


@pytest.mark.parametrize("name", ["", "a/b", "ecorp/ethan", "a b", "a@b", "-ethan", ".ethan", "ethan\n"])
@pytest.mark.parametrize("option", ["--demo-user", "--ops-user"])
def test_a_name_that_would_break_the_lookup_id_or_the_email_is_refused_before_anything_is_created(casdoor, option, name):
    with pytest.raises(SystemExit) as stop:
        casdoor([f"{option}={name}"])  # one argument: argparse would take a leading '-' for an option and refuse it before the script looks

    assert option in str(stop.value)
    assert FakeCasdoor.instances == [], "nothing may be created when the arguments are wrong"


# --- the record the REAL class sends --------------------------------------------------------------------------------------------------


class RecordingCasdoor(bootstrap.Casdoor):
    """The real `ensure_application`, with the two HTTP primitives replaced."""

    def __init__(self, existing: dict | None = None) -> None:  # the base __init__ logs in over HTTP; this must not
        self.existing = existing
        self.writes: list[tuple[str, dict]] = []

    def read(self, path: str, **params: str):
        return self.existing

    def write(self, path: str, body: dict, **params: str) -> None:
        self.writes.append((path, body))


def test_the_real_application_record_carries_the_token_lifetime_the_format_and_the_scope():
    casdoor = RecordingCasdoor()

    client_id = casdoor.ensure_application("api-ecorp", "ecorp", "api:chat", "chat API for ecorp", ["https://api.localhost:8443/"], 168, kind="API")

    path, body = casdoor.writes[0]
    assert path == "/api/add-application"
    assert body["expireInHours"] == 168
    assert body["refreshExpireInHours"] == bootstrap.REFRESH_HOURS
    assert body["tokenFormat"] == "JWT-Standard", "the default format carries ~85 claims, among them password-related fields"
    assert (body["category"], body["type"]) == ("Agent", "MCP"), "this is what turns custom scopes on (spec 017 research)"
    assert body["redirectUris"] == ["https://api.localhost:8443/"]
    assert body["scopes"][0]["name"] == "api:chat"
    assert body["displayName"] == "API: chat API for ecorp"
    assert body["clientId"] == client_id
    assert "authorization_code" in body["grantTypes"]


def test_a_rerun_keeps_the_client_id_and_the_secret_and_applies_the_new_lifetime():
    existing = {"clientId": "kept-id", "clientSecret": "kept-secret", "expireInHours": 1, "name": "api-ecorp", "owner": "admin"}
    casdoor = RecordingCasdoor(existing)

    client_id = casdoor.ensure_application("api-ecorp", "ecorp", "api:chat", "chat API for ecorp", ["https://api.localhost:8443/"], 168, kind="API")

    path, body = casdoor.writes[0]
    assert path == "/api/update-application"
    assert client_id == "kept-id", "a client already registered with the id must keep working"
    assert body["clientId"] == "kept-id" and body["clientSecret"] == "kept-secret"
    assert body["expireInHours"] == 168, "changing the setting and re-running updates the lifetime"


def test_a_named_client_id_is_used_on_creation_and_replaces_the_kept_one_on_a_rerun():
    created = RecordingCasdoor()
    assert created.ensure_application("api-ecorp", "ecorp", "api:chat", "chat API", ["https://api.localhost:8443/"], 168, kind="API", client_id="api-ecorp") == "api-ecorp"
    assert created.writes[0][1]["clientId"] == "api-ecorp"

    existing = {"clientId": "old-random-id", "clientSecret": "kept-secret", "expireInHours": 1, "name": "api-ecorp", "owner": "admin"}
    rerun = RecordingCasdoor(existing)
    assert rerun.ensure_application("api-ecorp", "ecorp", "api:chat", "chat API", ["https://api.localhost:8443/"], 168, kind="API", client_id="api-ecorp") == "api-ecorp"
    assert rerun.writes[0][1]["clientId"] == "api-ecorp" and rerun.writes[0][1]["clientSecret"] == "kept-secret", "the id changes, the secret does not"


def test_the_telegram_bots_login_return_is_registered_only_when_asked_and_is_the_path_the_bot_serves(casdoor):
    from app.adapters.inbound.channels.telegram import link as telegram_link

    assert bootstrap.TELEGRAM_LINK_CALLBACK_PATH == telegram_link.CALLBACK_PATH
    assert by_name(casdoor())["api-ecorp"]["redirect_uris"] == ["https://api.localhost:8443/", bootstrap.API_SMOKE_CALLBACK, "http://localhost:33420/callback"]

    uris = by_name(casdoor(["--telegram-link-url", "https://bot.example.com/"]))["api-ecorp"]["redirect_uris"]

    assert uris[-1] == "https://bot.example.com/telegram/link/callback"


class UsersById(RecordingCasdoor):
    """The real `ensure_user`, looking a user up by the id it builds (`<organization>/<name>`)."""

    def __init__(self, existing: set[str]) -> None:  # see RecordingCasdoor
        super().__init__()
        self.known = existing

    def read(self, path: str, **params: str):
        return {"name": params["id"]} if params["id"] in self.known else None


def test_the_real_user_record_carries_the_chosen_name_and_an_address_built_from_it():
    casdoor = UsersById(set())

    password = casdoor.ensure_user("ecorp", "priya")

    path, body = casdoor.writes[0]
    assert path == "/api/add-user" and password == body["password"] and len(password) >= 20
    assert (body["owner"], body["name"], body["displayName"], body["email"]) == ("ecorp", "priya", "priya", "priya@ecorp.localhost")


def test_a_user_is_found_by_its_name_so_a_renamed_one_is_created_again_under_the_old_name():
    # `ecorp/alice` was renamed to `ecorp/ethan` in Casdoor: the script given `ethan` leaves it alone, given `alice` it adds a second user.
    casdoor = UsersById({"ecorp/ethan"})

    assert casdoor.ensure_user("ecorp", "ethan") is None and casdoor.writes == [], "an existing user is left alone and no password is shown"
    assert casdoor.ensure_user("ecorp", "alice") is not None and [b["name"] for _, b in casdoor.writes] == ["alice"]


# --- the init file way (`--init-data-dir`): Casdoor's own declarative config, which the development stack mounts ------------------------------
# Measured on the pinned image (docs/ACCESS_PATTERNS.md): with `initDataMerge` each apply updates what exists, keeps a person's own password and a
# secret the file does not name, and replaces an array wholesale. These pin what the FILE must therefore contain, and not contain.


def render(tmp_path, *args: str) -> dict:
    import json

    bootstrap.main(["--init-data-dir", str(tmp_path), *args])
    return json.loads((tmp_path / bootstrap.INIT_FILE).read_text(encoding="utf-8"))


def file_apps(document: dict) -> dict[str, dict]:
    return {a["name"]: a for a in document["applications"]}


def test_the_init_file_way_needs_no_admin_password_and_never_calls_the_admin_api(monkeypatch, tmp_path):
    FakeCasdoor.instances = []
    monkeypatch.setattr(bootstrap, "Casdoor", FakeCasdoor)
    monkeypatch.delenv("CASDOOR_ADMIN_PASSWORD", raising=False)

    document = render(tmp_path)

    assert FakeCasdoor.instances == [], "the file is applied by Casdoor itself: no login, no API"
    assert [o["name"] for o in document["organizations"]] == ["ecorp", "ops-team"]
    assert set(file_apps(document)) == {"mcp-ecorp", "api-ecorp", "mcp-ops"}


def test_the_file_holds_the_records_the_api_way_sends_so_the_two_cannot_drift(casdoor, tmp_path):
    api = file_apps(render(tmp_path))["api-ecorp"]
    sent = RecordingCasdoor()
    sent.ensure_application("api-ecorp", "ecorp", "api:chat", "chat API for ecorp", api["redirectUris"], 168, kind="API", client_id="api-ecorp")

    body = {key: value for key, value in sent.writes[0][1].items() if key not in ("clientId", "clientSecret")}
    assert {key: value for key, value in api.items() if key != "clientId"} == body, "one definition, two deliveries"


def test_a_client_id_in_the_file_is_the_applications_name_and_no_secret_is_written(casdoor, tmp_path):
    apps = file_apps(render(tmp_path, "--tenant", "ecorp", "--tenant", "other-co"))

    assert {name: app["clientId"] for name, app in apps.items()} == {name: name for name in apps}, "a declarative file names its ids; `api-<tenant>` is the rule the web page uses"
    assert not [name for name, app in apps.items() if "clientSecret" in app], "Casdoor makes the secret when it creates the application, and merge keeps it: a file must not set one"


def test_the_file_never_sets_what_an_operator_sets_on_a_person(casdoor, tmp_path):
    """Merge writes every key the file names on each apply and each restart. Naming `isForbidden` would re-enable a demo user someone disabled (deploy/README.md
    tells an operator to), and naming `isAdmin` would demote an admin."""
    people = render(tmp_path, "--tenant", "ecorp", "--tenant", "other-co")["users"]

    assert [(u["owner"], u["name"]) for u in people] == [("ecorp", "ethan"), ("other-co", "ethan"), ("ops-team", "oncall")]
    for person in people:
        assert not set(bootstrap.OPERATOR_OWNED_USER_FIELDS) & set(person), person["name"]


def test_a_second_render_is_byte_identical_and_keeps_every_initial_password(casdoor, tmp_path):
    first = render(tmp_path)
    before = (tmp_path / bootstrap.INIT_FILE).read_bytes()

    second = render(tmp_path)

    assert (tmp_path / bootstrap.INIT_FILE).read_bytes() == before, "Casdoor compares the content: an unchanged file is not applied again"
    assert [u["password"] for u in second["users"]] == [u["password"] for u in first["users"]]
    assert all(len(u["password"]) >= 20 for u in first["users"])


def test_a_changed_argument_changes_the_file_and_keeps_the_people_known_password(casdoor, tmp_path):
    first = render(tmp_path)

    second = render(tmp_path, "--token-hours", "24")

    assert {a["expireInHours"] for a in second["applications"]} == {24} and {a["expireInHours"] for a in first["applications"]} == {168}
    assert [u["password"] for u in second["users"]] == [u["password"] for u in first["users"]]


def test_the_redirect_uris_are_listed_in_full_every_time_because_merge_replaces_an_array(casdoor, tmp_path):
    with_bot = file_apps(render(tmp_path, "--telegram-link-url", "https://bot.example.com"))["api-ecorp"]["redirectUris"]
    without = file_apps(render(tmp_path))["api-ecorp"]["redirectUris"]

    assert with_bot[-1] == "https://bot.example.com/telegram/link/callback"
    assert without == with_bot[:-1], "what the file does not list is gone from Casdoor at the next apply: the arguments are the whole truth"


def test_the_file_and_its_directory_are_private_because_it_holds_passwords(casdoor, tmp_path):
    target = tmp_path / "casdoor-init"

    render(target)

    assert (target / bootstrap.INIT_FILE).stat().st_mode & 0o777 == 0o600
    assert target.stat().st_mode & 0o777 == 0o700
    assert not list(target.glob(".*.tmp")), "the rename leaves no temporary file in the directory the container mounts"


def test_a_file_that_is_not_ours_is_replaced_not_trusted(casdoor, tmp_path):
    (tmp_path / bootstrap.INIT_FILE).write_text("[1, 2, 3]", encoding="utf-8")

    document = render(tmp_path)

    assert len(document["users"]) == 2 and all(u["password"] for u in document["users"])


def test_a_directory_that_cannot_be_written_exits_with_a_message_not_a_traceback(casdoor, tmp_path):
    (tmp_path / "in-the-way").write_text("", encoding="utf-8")

    with pytest.raises(SystemExit) as stop:
        bootstrap.main(["--init-data-dir", str(tmp_path / "in-the-way" / "casdoor-init")])

    assert str(stop.value).startswith("front-door-bootstrap:")


def test_the_output_says_where_the_file_is_and_that_the_passwords_are_initial_ones(casdoor, tmp_path, capsys):
    render(tmp_path, "--demo-user", "priya")

    out = capsys.readouterr().out
    assert "log in as ecorp/priya  password: " in out and "generated-password" not in out, "the passwords are the file's, not a recorder's"
    assert f"Wrote {tmp_path / bootstrap.INIT_FILE}" in out
    assert "INITIAL" in out and "keeps the new one" in out
    assert "claude mcp add --transport http --callback-port 33418 --client-id mcp-ecorp ecorp" in out, "an MCP client's id is its application's name, printed"
