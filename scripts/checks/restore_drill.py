"""A restore drill: take the production backups of a seeded stack and restore them into a server that has never seen the data (PRODUCTION_REVIEW.md R26).

    make restore-drill                     # python -m scripts.checks.restore_drill [--threads N] [--sessions N] [--points N] [--redis-keys N]

A backup nobody has restored is a hope. This starts throwaway containers from the SHIPPED images (Postgres 16 initialised by `postgres-init/`, Postgres 16 for Casdoor,
Qdrant, Redis Stack), seeds each with data of a size you choose, runs the SAME commands `deploy/backup/backup.sh` runs (`pg_dump --format=custom`, the Qdrant snapshot
tool, `redis-cli --rdb -`), starts a SECOND set of containers the way a rebuilt droplet would be (empty, initialised from the repository), restores with the commands
`deploy/README.md` gives, and checks the result: row counts and a checksum over the rows, the points and a search through the restored alias, and the Redis keys,
including after turning AOF on again. It prints how long each backup and restore took.

The numbers are THIS machine's, with the data sizes it prints: not a droplet's. Run it with sizes near your own to get a timing worth planning around. It needs Docker
and nothing else running; it stops every container it starts.

EXIT CODES. 0 every component restored and verified | 1 something did not match or failed | 2 usage.
"""
import argparse
import asyncio
import platform
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import psycopg
import redis

from app.bootstrap import wire

REPO = Path(__file__).resolve().parents[2]
PG_PASSWORD = "drill"
REDIS_ARGS = "--appendonly yes --appendfsync everysec --maxmemory 192mb --maxmemory-policy noeviction"  # what docker-compose.prod.yml gives `redis`
EXIT_OK, EXIT_FAILED, EXIT_USAGE = 0, 1, 2


def _compose_image(service: str) -> str:
    import yaml

    return str(yaml.safe_load((REPO / "deploy" / "compose" / "docker-compose.yml").read_text(encoding="utf-8"))["services"][service]["image"])


def _prod_service(service: str) -> dict[str, Any]:
    import yaml

    loaded: dict[str, Any] = yaml.safe_load((REPO / "deploy" / "compose" / "docker-compose.prod.yml").read_text(encoding="utf-8"))["services"][service]
    return loaded


def _prod_image(service: str) -> str:
    return str(_prod_service(service)["image"])


def _prod_postgres_args() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """`(docker run -e flags, the server command)` of the production `postgres` service, with the drill's password for the one secret. A drill that started Postgres its
    own way would not notice when production's own settings stop a new volume from initialising (they did: PRODUCTION_REVIEW.md R26)."""
    service = _prod_service("postgres")
    env = {**service["environment"], "POSTGRES_PASSWORD": PG_PASSWORD}
    flags = tuple(arg for key, value in env.items() for arg in ("-e", f"{key}={value}"))
    return flags, tuple(service["command"][1:])  # the image's own entrypoint supplies `postgres` first


@dataclass
class Row:
    component: str
    size: str
    backup_seconds: float = 0.0
    restore_seconds: float = 0.0
    problems: list[str] = field(default_factory=list)


def _docker(*args: str, stdout: int | None = subprocess.PIPE, stdin: int | None = None, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["docker", *args], stdout=stdout, stderr=subprocess.PIPE, stdin=stdin, check=check)


@contextmanager
def container(name: str, image: str, *run_args: str, command: tuple[str, ...] = ()) -> Iterator[str]:
    """Starts a throwaway container (removed on stop), yields the host port its first published port is on, and stops it."""
    _docker("run", "-d", "--rm", "--name", name, *run_args, image, *command)
    try:
        port = _docker("port", name).stdout.decode().splitlines()[0].rsplit(":", 1)[1]
        yield port
    finally:
        subprocess.run(["docker", "stop", "-t", "3", name], capture_output=True)


def _wait(probe: Callable[[], object], what: str, seconds: int = 120) -> None:
    """Calls `probe` until it stops raising."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            probe()
            return
        except Exception:  # noqa: BLE001 - any failure means "not up yet"
            if time.monotonic() > deadline:
                raise RuntimeError(f"{what} did not come up in {seconds} s") from None
            time.sleep(0.5)


def _pg_url(port: str, database: str, user: str = "langfuse") -> str:
    return f"postgresql://{user}:{PG_PASSWORD}@127.0.0.1:{port}/{database}"


def _pg_ready(port: str, database: str, table: str, user: str = "langfuse") -> None:
    with psycopg.connect(_pg_url(port, database, user), connect_timeout=2) as conn:
        conn.execute(f"SELECT 1 FROM {table} LIMIT 1")


def _scalar(url: str, sql: str) -> object:
    with psycopg.connect(url) as conn:
        row = conn.execute(sql).fetchone()
        return row[0] if row else None


def _dump(name: str, user: str, database: str, target: Path) -> float:
    start = time.monotonic()
    with target.open("wb") as handle:
        _docker("exec", name, "pg_dump", "-U", user, "--format=custom", database, stdout=handle.fileno())
    return time.monotonic() - start


def _restore(name: str, user: str, database: str, source: Path) -> tuple[float, str]:
    start = time.monotonic()
    with source.open("rb") as handle:
        done = _docker("exec", "-i", name, "pg_restore", "-U", user, "--clean", "--if-exists", "-d", database, stdout=subprocess.DEVNULL, stdin=handle.fileno(), check=False)
    return time.monotonic() - start, done.stderr.decode().strip() if done.returncode else ""


def _mb(path: Path) -> str:
    return f"{path.stat().st_size / 1_048_576:.1f} MB"


# --- Postgres: appdata, checkpointer, litellm ---------------------------------------------------------------------------------------------------


async def _seed_checkpoints(url: str, threads: int) -> None:
    from langgraph.checkpoint.base import empty_checkpoint

    from app.adapters.outbound.postgres import checkpointing

    setattr(checkpointing, "CHECKPOINTER_DATABASE_URL", url)  # noqa: B010 - the adapter reads its own module-level name when it opens
    adapter = checkpointing.PgCheckpointing()
    saver = await adapter.open()
    try:
        for t in range(threads):
            config: Any = {"configurable": {"thread_id": f"drill-{t}", "checkpoint_ns": ""}}
            for step in range(5):
                checkpoint: Any = {**empty_checkpoint(), "channel_values": {"messages": [f"message {i} " * 20 for i in range(step + 1)]}, "channel_versions": {"messages": str(step + 1)}}
                metadata: Any = {"source": "loop", "step": step, "writes": {}, "parents": {}}
                config = await saver.aput(config, checkpoint, metadata, {"messages": str(step + 1)})
            await saver.aput_writes(config, [("messages", "pending")], task_id=f"task-{t}")
    finally:
        await adapter.close()


def _pg_fingerprint(port: str) -> dict[str, object]:
    appdata, checkpointer = _pg_url(port, "appdata"), _pg_url(port, "checkpointer")
    return {
        "chat_sessions": _scalar(appdata, "SELECT count(*) FROM chat_sessions"),
        "chat_sessions md5": _scalar(appdata, "SELECT md5(string_agg(thread_id || '|' || tenant || '|' || principal || '|' || title, ',' ORDER BY thread_id)) FROM chat_sessions"),
        "checkpoints": _scalar(checkpointer, "SELECT count(*) FROM checkpoints"),
        "checkpoint_blobs": _scalar(checkpointer, "SELECT count(*) FROM checkpoint_blobs"),
        "checkpoint_writes": _scalar(checkpointer, "SELECT count(*) FROM checkpoint_writes"),
        "checkpoints md5": _scalar(checkpointer, "SELECT md5(string_agg(thread_id || '|' || checkpoint_id, ',' ORDER BY thread_id, checkpoint_id)) FROM checkpoints"),
    }


def drill_postgres(work: Path, sessions: int, threads: int) -> list[Row]:
    image = _prod_image("postgres")
    init = f"{REPO / 'postgres-init'}:/docker-entrypoint-initdb.d:ro"
    env, server_command = _prod_postgres_args()
    casdoor_env = ("-e", "POSTGRES_USER=casdoor", "-e", f"POSTGRES_PASSWORD={PG_PASSWORD}", "-e", "POSTGRES_DB=casdoor")
    rows = [Row("postgres: appdata + checkpointer + litellm", ""), Row("postgres: casdoor", "")]
    with (
        container("drill-pg-src", image, *env, "-v", init, "-p", "127.0.0.1::5432", command=server_command) as src,
        container("drill-casdoor-src", "postgres:16-alpine", *casdoor_env, "-p", "127.0.0.1::5432") as casdoor_src,
    ):
        _wait(lambda: _pg_ready(src, "appdata", "chat_sessions"), "the source Postgres")
        _wait(lambda: _pg_ready(casdoor_src, "casdoor", "pg_catalog.pg_tables", "casdoor"), "the source Casdoor database")
        with psycopg.connect(_pg_url(src, "appdata"), autocommit=True) as conn:
            conn.execute(
                "INSERT INTO chat_sessions (thread_id, tenant, principal, title, domain) "
                "SELECT 'thread-' || g, 'tenant-' || (g %% 20), 'person-' || (g %% 500), 'title ' || g, 'ecorp' FROM generate_series(1, %s) g",
                (sessions,),
            )
        asyncio.run(_seed_checkpoints(_pg_url(src, "checkpointer"), threads))
        with psycopg.connect(_pg_url(casdoor_src, "casdoor", "casdoor"), autocommit=True) as conn:
            conn.execute("CREATE TABLE users (id serial PRIMARY KEY, name text, hash text)")
            conn.execute("INSERT INTO users (name, hash) SELECT 'user-' || g, md5(g::text) FROM generate_series(1, 2000) g")
        before = _pg_fingerprint(src)
        casdoor_before = _scalar(_pg_url(casdoor_src, "casdoor", "casdoor"), "SELECT md5(string_agg(name || hash, ',' ORDER BY id)) FROM users")

        dumps = {db: work / f"{db}.dump" for db in ("appdata", "checkpointer", "litellm")}
        rows[0].backup_seconds = sum(_dump("drill-pg-src", "langfuse", db, path) for db, path in dumps.items())
        casdoor_dump = work / "casdoor.dump"
        rows[1].backup_seconds = _dump("drill-casdoor-src", "casdoor", "casdoor", casdoor_dump)
        rows[0].size = f"{sessions} sessions, {threads * 5} checkpoints; dumps {', '.join(_mb(p) for p in dumps.values())}"
        rows[1].size = f"2000 users; dump {_mb(casdoor_dump)}"

    # The "rebuilt droplet": new, empty servers initialised from the repository, never having seen the data above.
    with (
        container("drill-pg-dst", image, *env, "-v", init, "-p", "127.0.0.1::5432", command=server_command) as dst,
        container("drill-casdoor-dst", "postgres:16-alpine", *casdoor_env, "-p", "127.0.0.1::5432") as casdoor_dst,
    ):
        _wait(lambda: _pg_ready(dst, "appdata", "chat_sessions"), "the restore-target Postgres")
        _wait(lambda: _pg_ready(casdoor_dst, "casdoor", "pg_catalog.pg_tables", "casdoor"), "the restore-target Casdoor database")
        assert _scalar(_pg_url(dst, "appdata"), "SELECT count(*) FROM chat_sessions") == 0, "the target must be empty"
        for db, path in dumps.items():
            seconds, errors = _restore("drill-pg-dst", "langfuse", db, path)
            rows[0].restore_seconds += seconds
            if errors:
                rows[0].problems.append(f"pg_restore {db}: {errors[:300]}")
        seconds, errors = _restore("drill-casdoor-dst", "casdoor", "casdoor", casdoor_dump)
        rows[1].restore_seconds = seconds
        if errors:
            rows[1].problems.append(f"pg_restore casdoor: {errors[:300]}")
        after = _pg_fingerprint(dst)
        for key, value in before.items():
            if after[key] != value:
                rows[0].problems.append(f"{key}: {value!r} before, {after[key]!r} after")
        casdoor_after = _scalar(_pg_url(casdoor_dst, "casdoor", "casdoor"), "SELECT md5(string_agg(name || hash, ',' ORDER BY id)) FROM users")
        if casdoor_after != casdoor_before:
            rows[1].problems.append("casdoor users differ after the restore")
    return rows


# --- Qdrant -------------------------------------------------------------------------------------------------------------------------------------


async def _qdrant_phase(source_url: str, target_url: str, work: Path, points: int) -> Row:
    import random

    from app.adapters.outbound.qdrant import qdrant_store
    from scripts.tools import qdrant_backup

    row = Row("qdrant: a collection behind its -live alias", f"{points} points of 768 dimensions")

    async def use(url: str) -> None:
        await qdrant_store.close_client()
        setattr(qdrant_store, "QDRANT_URL", url)  # noqa: B010 - each module reads its own module-level name
        setattr(qdrant_backup, "QDRANT_URL", url)  # noqa: B010
        qdrant_store.reset_identity_cache()

    async def top3() -> list[str]:
        response = await qdrant_store.get_client().query_points(collection_name="docs-live", query=[0.01 * (i % 7) for i in range(768)], using=qdrant_store.DENSE_VECTOR_NAME, limit=3)
        return [str(p.id) for p in response.points]

    await use(source_url)
    rng = random.Random(7)
    await qdrant_store.create_physical("docs-drill0001", 768)
    for start in range(0, points, 500):
        batch = [
            qdrant_store.build_point(str(uuid.UUID(int=n)), [rng.random() for _ in range(768)], {"tenant": f"t{n % 5}", "kind": "document", "source": f"upload:f{n % 50}.pdf", "text": f"chunk {n}"})
            for n in range(start, min(start + 500, points))
        ]
        await qdrant_store.get_client().upsert("docs-drill0001", batch)
    await qdrant_store.repoint_live("docs", "docs-drill0001")
    expected = await top3()

    started = time.monotonic()
    await qdrant_backup.backup(work)
    row.backup_seconds = time.monotonic() - started
    row.size += f"; snapshot {_mb(work / 'qdrant-docs-drill0001.snapshot')}"

    await use(target_url)
    started = time.monotonic()
    await qdrant_backup.restore(work)
    row.restore_seconds = time.monotonic() - started
    count = (await qdrant_store.get_client().count("docs-drill0001", exact=True)).count
    if count != points:
        row.problems.append(f"{count} points after the restore, expected {points}")
    if await top3() != expected:
        row.problems.append("the same query returned different points through the restored alias")
    schema = (await qdrant_store.get_client().get_collection("docs-drill0001")).payload_schema
    if not {"tenant", "kind", "source"} <= set(schema):
        row.problems.append(f"payload indexes missing after the restore: {sorted(schema)}")
    await qdrant_store.close_client()
    return row


def drill_qdrant(work: Path, points: int) -> Row:
    image = _compose_image("qdrant")

    def up(port: str) -> None:
        import httpx

        httpx.get(f"http://127.0.0.1:{port}/collections", timeout=2).raise_for_status()

    with container("drill-qdrant-src", image, "-p", "127.0.0.1::6333") as src, container("drill-qdrant-dst", image, "-p", "127.0.0.1::6333") as dst:
        _wait(lambda: up(src), "the source Qdrant")
        _wait(lambda: up(dst), "the restore-target Qdrant")
        return asyncio.run(_qdrant_phase(f"http://127.0.0.1:{src}", f"http://127.0.0.1:{dst}", work, points))


# --- Redis --------------------------------------------------------------------------------------------------------------------------------------


def _redis(port: str) -> redis.Redis:
    return redis.Redis(host="127.0.0.1", port=int(port), decode_responses=True)


def _redis_fingerprint(client: redis.Redis) -> dict[str, object]:
    return {
        "keys": client.dbsize(),
        "stream entries": client.xlen("agent:requests:ecorp"),
        "revocation": client.get("revoked:acme:alice"),
        "lock": client.get("agent:lock:thread-1"),
    }


def drill_redis(work: Path, keys: int) -> Row:
    image = _prod_image("redis")
    row = Row("redis: the queue, locks and revocation list", f"{keys} keys and a {keys}-entry stream")
    flags = ("-e", f"REDIS_ARGS={REDIS_ARGS}")
    with container("drill-redis-src", image, *flags, "-p", "127.0.0.1::6379") as src:
        client = _redis(src)
        _wait(client.ping, "the source Redis")
        for n in range(keys):
            client.set(f"agent:cancel:thread-{n}", "1")
            client.xadd("agent:requests:ecorp", {"payload": f"job {n}"})
        client.set("revoked:acme:alice", "1700000000")
        client.set("agent:lock:thread-1", "token")
        before = _redis_fingerprint(client)
        dump = work / "redis.rdb"
        started = time.monotonic()
        with dump.open("wb") as handle:
            _docker("exec", "drill-redis-src", "redis-cli", "--rdb", "-", stdout=handle.fileno())
        row.backup_seconds = time.monotonic() - started
        row.size += f"; dump {_mb(dump)}"

    # Restore as deploy/README.md says: start on the RDB with AOF OFF so it is loaded, turn AOF on in the running server, wait for the rewrite, then restart with the real flags.
    data = work / "redis-data"
    data.mkdir()
    (data / "dump.rdb").write_bytes(dump.read_bytes())
    started = time.monotonic()
    with container("drill-redis-load", image, "-e", "REDIS_ARGS=--appendonly no", "-v", f"{data}:/data", "-p", "127.0.0.1::6379") as load:
        client = _redis(load)
        _wait(client.ping, "the Redis loading the RDB")
        loaded = _redis_fingerprint(client)
        client.config_set("appendonly", "yes")

        def rewritten() -> None:
            info = client.info("persistence")
            if info["aof_rewrite_in_progress"] or info["aof_rewrite_scheduled"] or info["aof_last_bgrewrite_status"] != "ok":
                raise RuntimeError("AOF rewrite not finished")

        _wait(rewritten, "the AOF rewrite")
    with container("drill-redis-final", image, *flags, "-v", f"{data}:/data", "-p", "127.0.0.1::6379") as final:
        client = _redis(final)
        _wait(client.ping, "the restored Redis")
        after = _redis_fingerprint(client)
        row.restore_seconds = time.monotonic() - started
        for stage, seen in (("after loading the RDB", loaded), ("after switching AOF on and restarting with the production flags", after)):
            for key, value in before.items():
                if seen[key] != value:
                    row.problems.append(f"{key} {stage}: {value!r} before, {seen[key]!r} after")
    return row


# --- the drill ----------------------------------------------------------------------------------------------------------------------------------


def run(args: argparse.Namespace) -> int:
    wire()  # the checkpointer and Qdrant helpers it uses are the application's own adapters
    info = _docker("info", "--format", "{{.NCPU}} CPUs, {{.MemTotal}} bytes").stdout.decode().strip()
    print(f"machine: {platform.platform()}; Docker: {info}")
    rows: list[Row] = []
    with tempfile.TemporaryDirectory(prefix="restore-drill-") as tmp:
        work = Path(tmp)
        (work / "qdrant").mkdir()
        (work / "redis").mkdir()
        phases: list[tuple[str, Callable[[], list[Row]]]] = [
            ("postgres", lambda: drill_postgres(work, args.sessions, args.threads)),
            ("qdrant", lambda: [drill_qdrant(work / "qdrant", args.points)]),
            ("redis", lambda: [drill_redis(work / "redis", args.redis_keys)]),
        ]
        for label, phase in phases:
            print(f"... {label}", flush=True)
            try:
                rows.extend(phase())
            except Exception as exc:  # noqa: BLE001 - report which component failed and still print what did restore
                rows.append(Row(label, "", problems=[f"{type(exc).__name__}: {str(exc)[:300]}"]))
    print(f"\n{'component':52} {'backup s':>9} {'restore s':>10}  data")
    for row in rows:
        print(f"{row.component:52} {row.backup_seconds:9.1f} {row.restore_seconds:10.1f}  {row.size}  {'VERIFIED' if not row.problems else 'FAILED'}")
        for problem in row.problems:
            print(f"    - {problem}")
    total_backup = sum(r.backup_seconds for r in rows)
    total_restore = sum(r.restore_seconds for r in rows)
    print(f"{'total':52} {total_backup:9.1f} {total_restore:10.1f}")
    return EXIT_OK if all(not r.problems for r in rows) else EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sessions", type=int, default=50_000, help="rows in chat_sessions (default 50000)")
    parser.add_argument("--threads", type=int, default=1_000, help="conversations with checkpoints, 5 steps each (default 1000)")
    parser.add_argument("--points", type=int, default=20_000, help="Qdrant points of 768 dimensions (default 20000)")
    parser.add_argument("--redis-keys", type=int, default=20_000, help="Redis keys, and stream entries (default 20000)")
    args = parser.parse_args(argv)
    if min(args.sessions, args.threads, args.points, args.redis_keys) < 1:
        parser.error("every size must be at least 1")
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
