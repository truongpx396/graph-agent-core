"""Create, idempotently, what the front door and the MCP gateway expect to find in Casdoor (specs/017, 018): `make
front-door-bootstrap` (named `mcp-gateway-bootstrap` while only the MCP gateway needed it, and still answering to it). The chat API and the MCP gateway
are resource servers and trust whatever the authorization server signed for them, which needs an APPLICATION per
published server whose tokens carry the right claims, and a user to log in as. A second run changes nothing it
shouldn't.

- one ORGANIZATION per tenant (default `ecorp`, which owns the seeded rows) and one for operators
  (`ops-team`). The token's `owner` claim is the organization, which the gateway reads as the tenant
  (`MCP_GATEWAY_TENANT_CLAIM`).
- one APPLICATION per organization and server: `mcp-<tenant>` (scope `ecorp:read`) and `mcp-ops` (scope
  `ops:read`). Category `Agent` / type `MCP` turns custom scopes on. Tokens are `JWT-Standard` (17 claims,
  none of the profile; the default format carries ~85 including `lastSigninIp`).
- (specs/018) one APPLICATION per tenant for the chat API: `api-<tenant>` (scope `api:chat`), a public client
  whose redirect URIs are the web page's root and the smoke check's callback, verified at the front door by
  authlimit.
- every access token lasts SEVEN DAYS by default (`--token-hours` or TOKEN_HOURS; spec 018 FR-022).
  Verification is offline, so a token can't be recalled: `make authlimit-revoke` bounds that, and a short
  access token plus a refresh token is the production recommendation. The MCP applications follow the same
  setting (spec 017 used one hour; revocation now covers the MCP routes).
- a demo USER per organization (`--demo-user`, default `ethan`; `--ops-user`, default `oncall`, for the operators'). The password is printed ONCE,
  when created; an existing user is left alone, by NAME: rename one in Casdoor and the next run, still given the old name, creates it again.

It prints the `claude mcp add` command per server, with a PRE-REGISTERED client and fixed callback port: this
Casdoor has dynamic client registration off by default (anyone reaching it could register a client) and no
Client ID Metadata Document support (both measured, specs/017 research.md), and it compares redirect URIs
exactly.

Two ways to deliver the same objects (one definition of each, below):

- `--init-data-dir DIR` (the development stack, `make front-door-bootstrap`): write Casdoor's OWN declarative file, `DIR/init_data.json`. The casdoor
  service reads it at start and again within seconds of a change (`initDataFile`, `initDataMerge`, `initDataWatchInterval`), so a fresh stack needs no
  second step and no admin password. Merge mode updates what exists and keeps a person's own password; Casdoor's default would delete and re-create it.
  Measured on the pinned image (docs/ACCESS_PATTERNS.md, pattern 103).
- without it (production, deploy/README.md step 9): call the admin API from INSIDE the gateway container (`docker compose exec`), where Casdoor is
  `http://casdoor:8000` (no TLS to trust; the admin API isn't published). Needs CASDOOR_ADMIN_PASSWORD.

Standard library, plus httpx for the API way only, nothing from `app`.
"""
import argparse
import json
import os
import re
import secrets
import sys
from pathlib import Path
from typing import Any, cast

try:
    import httpx
except ImportError:  # --init-data-dir needs only the standard library: the dev droplet runs it with a bare python3
    httpx = None  # type: ignore[assignment]  # the API way says so when it needs it (Casdoor.__init__)

ADMIN_ORG = "built-in"
ADMIN_USER = "admin"
DEFAULT_TOKEN_HOURS = 168  # seven days (spec 018, FR-022)
DEFAULT_DEMO_USER = "ethan"
DEFAULT_OPS_USER = "oncall"
# `<organization>/<name>` is the id the script looks a user up by and `<name>@<organization>.localhost` the email it gives them, so a name holds no slash, space or '@'.
USER_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
REFRESH_HOURS = 168
# The smoke check (scripts/checks/authlimit_smoke.py) signs in as a public client with this callback; the web page signs in with its own root URL.
API_SMOKE_CALLBACK = "http://localhost:33419/callback"
# Where the Telegram bot's browser returns after a login (app/adapters/inbound/channels/telegram/link.py::CALLBACK_PATH; a test holds the two equal: this script imports nothing from app/).
TELEGRAM_LINK_CALLBACK_PATH = "/telegram/link/callback"


INIT_FILE = "init_data.json"
# Set by an operator in Casdoor's UI and absent from the file on purpose: merge mode writes every key the file names on each apply, so naming them would
# re-enable a person someone disabled (and demote an admin) at the next restart. A new person is created with both false anyway.
OPERATOR_OWNED_USER_FIELDS = ("isAdmin", "isForbidden")


class CasdoorError(Exception):
    pass


# -- what to create: one definition, delivered by the admin API (`Casdoor`) or by the init file (`InitData`) -------------------------------------


def organization_record(name: str) -> dict[str, Any]:
    return {"owner": "admin", "name": name, "displayName": name, "passwordType": "bcrypt", "defaultApplication": "", "enableSoftDeletion": False}


def user_record(organization: str, name: str, password: str) -> dict[str, Any]:
    return {
        "owner": organization,
        "name": name,
        "displayName": name,
        "password": password,
        "email": f"{name}@{organization}.localhost",
        "type": "normal-user",
        "isAdmin": False,
        "isForbidden": False,
    }


def application_record(name: str, organization: str, scope: str, description: str, redirect_uris: list[str], token_hours: int, kind: str) -> dict[str, Any]:
    """Everything but the client id and secret, which each delivery decides."""
    return {
        "owner": "admin",
        "name": name,
        "displayName": f"{kind}: {description}",
        "organization": organization,
        "category": "Agent",
        "type": "MCP",
        "cert": "cert-built-in",
        "enablePassword": True,
        "enableSignUp": False,
        "grantTypes": ["authorization_code", "refresh_token"],
        "redirectUris": redirect_uris,
        "tokenFormat": "JWT-Standard",
        "expireInHours": token_hours,
        "refreshExpireInHours": REFRESH_HOURS,
        "scopes": [{"name": scope, "displayName": scope, "description": description}],
    }


class InitData:
    """The same `ensure_*` calls as `Casdoor`, collected into the file Casdoor reads itself (`initDataFile`) instead of sent to its admin API.

    A file is declarative, so two things differ from the API way. A client id is the application's name (`mcp-ecorp`, `api-ecorp`): it is not a secret,
    and there is no "generated once and kept" to remember. The client SECRET is left out: Casdoor makes one when it creates the application and merge
    keeps it. A person's password is generated once and kept IN the file across renders: it is only their initial password (Casdoor keeps whatever they
    change it to), but it makes a re-render byte-identical, so Casdoor sees no change."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.file = directory / INIT_FILE
        self.organizations: dict[str, dict[str, Any]] = {}
        self.applications: dict[str, dict[str, Any]] = {}
        self.users: dict[tuple[str, str], dict[str, Any]] = {}
        try:
            self.known = {(u["owner"], u["name"]): u["password"] for u in json.loads(self.file.read_text(encoding="utf-8")).get("users", []) if u.get("password")}
        except (OSError, ValueError, KeyError, AttributeError):  # no file yet, or one that is not ours: start over
            self.known = {}

    def ensure_organization(self, name: str) -> None:
        self.organizations[name] = organization_record(name)

    def ensure_user(self, organization: str, name: str) -> str:
        password = self.known.get((organization, name)) or secrets.token_urlsafe(18)
        record = user_record(organization, name, password)
        self.users[(organization, name)] = {key: value for key, value in record.items() if key not in OPERATOR_OWNED_USER_FIELDS}
        return password

    def ensure_application(
        self,
        name: str,
        organization: str,
        scope: str,
        description: str,
        redirect_uris: list[str],
        token_hours: int,
        kind: str = "MCP",
        client_id: str | None = None,
    ) -> str:
        client_id = client_id or name
        self.applications[name] = {**application_record(name, organization, scope, description, redirect_uris, token_hours, kind), "clientId": client_id}
        return client_id

    def write(self) -> None:
        """Atomically (a rename inside the directory the container mounts, never an in-place write it could read half of), mode 0600: it holds passwords."""
        document = {
            "organizations": list(self.organizations.values()),
            "applications": list(self.applications.values()),
            "users": list(self.users.values()),
        }
        text = json.dumps(document, indent=2) + "\n"
        try:
            if self.file.read_text(encoding="utf-8") == text:
                return
        except OSError:
            pass
        self.directory.mkdir(parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        temporary = self.directory / f".{INIT_FILE}.tmp"
        with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, self.file)


class Casdoor:
    """The few admin endpoints this needs. Every call checks Casdoor's own `status` field: it answers HTTP 200 for failures."""

    def __init__(self, base_url: str, admin_password: str) -> None:
        if httpx is None:
            raise CasdoorError("the admin API way needs httpx (pip install httpx); `--init-data-dir` does not")
        self.http = httpx.Client(base_url=base_url, timeout=20.0)
        reply = self.http.post(
            "/api/login",
            json={
                "application": "app-built-in",
                "organization": ADMIN_ORG,
                "username": ADMIN_USER,
                "password": admin_password,
                "type": "login",
                "autoSignin": True,
            },
        ).json()
        if reply.get("status") != "ok":
            raise CasdoorError(f"admin login failed ({reply.get('msg') or reply.get('code')}); stopping rather than retrying (Casdoor locks after 5)")

    def read(self, path: str, **params: str) -> dict[str, Any]:
        reply = self.http.get(path, params=params).json()
        if reply.get("status") == "error":
            raise CasdoorError(f"GET {path}: {reply.get('msg')}")
        return cast("dict[str, Any]", reply.get("data"))

    def write(self, path: str, body: dict[str, Any], **params: str) -> None:
        reply = self.http.post(path, json=body, params=params).json()
        if reply.get("status") != "ok":
            raise CasdoorError(f"POST {path}: {reply.get('msg')}")

    # -- what to ensure --------------------------------------------------------------------------------------------------------

    def ensure_organization(self, name: str) -> None:
        if self.read("/api/get-organization", id=f"admin/{name}"):
            return
        self.write("/api/add-organization", organization_record(name))
        print(f"created organization {name}")

    def ensure_user(self, organization: str, name: str) -> str | None:
        """The generated password if the user was created now, else None."""
        if self.read("/api/get-user", id=f"{organization}/{name}"):
            return None
        password = secrets.token_urlsafe(18)
        self.write("/api/add-user", user_record(organization, name, password))
        return password

    def ensure_application(
        self,
        name: str,
        organization: str,
        scope: str,
        description: str,
        redirect_uris: list[str],
        token_hours: int,
        kind: str = "MCP",
        client_id: str | None = None,
    ) -> str:
        """Create or update the application; returns its client id.

        Normally the id is generated once and KEPT on a re-run, so a client already registered with it still works. When the caller NAMES an id
        (`client_id`, used for the chat API's applications) that id is the one the application has, created or changed to it: the built-in web
        page finds a tenant's client by the rule `api-<tenant>` (app/adapters/inbound/api/web_auth.py) instead of being told every tenant's random id, and a
        client id is not a secret."""
        existing = self.read("/api/get-application", id=f"admin/{name}")
        record = application_record(name, organization, scope, description, redirect_uris, token_hours, kind)
        if existing:
            kept = client_id or existing["clientId"]
            record = {**existing, **record, "clientId": kept, "clientSecret": existing["clientSecret"]}
            self.write("/api/update-application", record, id=f"admin/{name}")
            return kept
        client_id = client_id or secrets.token_hex(10)
        self.write("/api/add-application", {**record, "clientId": client_id, "clientSecret": secrets.token_hex(20)})
        print(f"created application {name}")
        return client_id


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Create the Casdoor organizations, applications and demo users the MCP gateway expects.")
    parser.add_argument("--tenant", action="append", default=[], help="a tenant (Casdoor organization) to create an Ecorp-directory application for; repeatable (default: ecorp)")
    parser.add_argument("--ops-org", default=os.environ.get("MCP_GATEWAY_OPS_TENANTS", "ops-team").split(",")[0].strip() or "ops-team")
    parser.add_argument(
        "--demo-user",
        default=DEFAULT_DEMO_USER,
        help=f"the user created in every tenant's organization (default {DEFAULT_DEMO_USER}). An existing user is found by this name: if you renamed one in Casdoor, pass its new name, "
        "or the next run creates the old name again",
    )
    parser.add_argument("--ops-user", default=DEFAULT_OPS_USER, help=f"the user created in the operators' organization (default {DEFAULT_OPS_USER})")
    parser.add_argument("--callback-port", type=int, default=33418, help="the fixed port of the client's OAuth callback (claude mcp add --callback-port)")
    parser.add_argument("--cli-callback-port", type=int, default=33420, help="the fixed port of `make chat`'s sign-in callback (CHANNEL_CLI_CALLBACK_PORT, specs/020)")
    parser.add_argument(
        "--telegram-link-url",
        default="",
        help="the Telegram bot's public https address (TELEGRAM_LINK_PUBLIC_URL, specs/021): its /telegram/link/callback is registered as a redirect URI of every tenant's chat client, "
        "which the authorization server compares exactly. Leave it out when the bot does not link accounts",
    )
    parser.add_argument(
        "--token-hours",
        type=int,
        default=int(os.environ.get("TOKEN_HOURS") or DEFAULT_TOKEN_HOURS),
        help="how long an access token lasts, for every application this creates (default 168 = seven days; TOKEN_HOURS sets the default). "
        "A shorter lifetime with a refresh token is the production recommendation",
    )
    parser.add_argument(
        "--init-data-dir",
        default="",
        help="write Casdoor's own init file, DIR/init_data.json, instead of calling its admin API (the development stack: `make front-door-bootstrap`). Casdoor applies it at "
        "start and within seconds of a change, and no admin password is needed. The file lists exactly what is given: Casdoor never deletes what it no longer names",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.token_hours <= 8760:
        sys.exit("front-door-bootstrap: --token-hours must be between 1 and 8760")
    for option, user in (("--demo-user", args.demo_user), ("--ops-user", args.ops_user)):
        if not USER_NAME.fullmatch(user):
            sys.exit(f"front-door-bootstrap: {option} must be letters, digits, '.', '_' or '-' and start with a letter or digit (got {user!r})")
    tenants = args.tenant or ["ecorp"]

    init_dir = args.init_data_dir
    password = os.environ.get("CASDOOR_ADMIN_PASSWORD", "")
    if not init_dir and not password:
        sys.exit("CASDOOR_ADMIN_PASSWORD is not set (the admin API way needs it, as deploy/README.md step 9 passes it; `--init-data-dir` does not)")
    public = os.environ.get("MCP_GATEWAY_PUBLIC_URL", "https://mcp.localhost:8443").rstrip("/")
    callback = f"http://localhost:{args.callback_port}/callback"
    cli_callback = f"http://localhost:{args.cli_callback_port}/callback"
    link_url = args.telegram_link_url.strip().rstrip("/")
    link_callbacks = [f"{link_url}{TELEGRAM_LINK_CALLBACK_PATH}"] if link_url else []
    api_public = os.environ.get("AUTHLIMIT_AUDIENCE", "https://api.localhost:8443").rstrip("/")  # the chat API's resource identifier (the token's audience)

    errors: tuple[type[Exception], ...] = (CasdoorError, OSError, httpx.HTTPError) if httpx else (CasdoorError, OSError)
    try:
        casdoor: Casdoor | InitData = InitData(Path(init_dir)) if init_dir else Casdoor(os.environ.get("CASDOOR_URL", "http://casdoor:8000"), password)
        commands = []
        api_clients = []
        for tenant in tenants:
            casdoor.ensure_organization(tenant)
            client_id = casdoor.ensure_application(f"mcp-{tenant}", tenant, "ecorp:read", f"Ecorp directory for {tenant}", [callback], args.token_hours)
            user_password = casdoor.ensure_user(tenant, args.demo_user)
            commands.append(("ecorp" if tenant == "ecorp" else f"ecorp-{tenant}", tenant, "/ecorp/mcp", client_id, args.demo_user, user_password))
            # The chat API (specs/018): the page signs in at its own root URL; the smoke check signs in at its own callback.
            api_client_id = casdoor.ensure_application(
                f"api-{tenant}",
                tenant,
                "api:chat",
                f"chat API for {tenant}",
                [f"{api_public}/", API_SMOKE_CALLBACK, cli_callback, *link_callbacks],  # the page, the smoke check, `make chat`, and the Telegram bot's login return
                args.token_hours,
                kind="API",
                client_id=f"api-{tenant}",  # the rule the web page uses to find a tenant's client (app/adapters/inbound/api/web_auth.py::CLIENT_ID_PREFIX)
            )
            api_clients.append((tenant, api_client_id))
        casdoor.ensure_organization(args.ops_org)
        client_id = casdoor.ensure_application("mcp-ops", args.ops_org, "ops:read", "operations metrics and incidents", [callback], args.token_hours)
        commands.append(("ops", args.ops_org, "/ops/mcp", client_id, args.ops_user, casdoor.ensure_user(args.ops_org, args.ops_user)))
        if isinstance(casdoor, InitData):
            casdoor.write()
    except errors as exc:
        sys.exit(f"front-door-bootstrap: {exc}")

    issuer = os.environ.get("MCP_GATEWAY_ISSUER", "https://auth.localhost:8443").rstrip("/")
    local = ".localhost" in issuer  # the development stack's names, signed by Caddy's own CA; a real deployment's are not
    print("\nRegister each server in Claude Code (a pre-registered client" + ("; trust the local CA first: make front-door-ca" if local else "") + "):\n")
    for label, organization, path, client_id, user, user_password in commands:
        print(f"  claude mcp add --transport http --callback-port {args.callback_port} --client-id {client_id} {label} {public}{path}")
        shown = user_password if user_password else "(already existed: password unchanged)"
        print(f"    log in as {organization}/{user}  password: {shown}\n")
    print(f"The chat API (resource {api_public}, scope api:chat), one public client per tenant; sign in as <tenant>/{args.demo_user}:\n")
    for tenant, api_client_id in api_clients:
        print(f"  {tenant}: client id {api_client_id}")
    print(f"\nAccess tokens last {args.token_hours} hours. `make authlimit-revoke TENANT=<tenant> SUB=<subject>` voids a person's tokens at once.")
    print(f"The browser login is {issuer} (Casdoor" + (", signed by Caddy's local CA" if local else "") + ").")
    if init_dir:
        print(f"\nWrote {Path(init_dir) / INIT_FILE}: Casdoor reads it at start and again within seconds of a change (its log says `Applied the changed init data file`).")
        print("The passwords above are each person's INITIAL one: a person who changed theirs keeps the new one.")


if __name__ == "__main__":
    main()
