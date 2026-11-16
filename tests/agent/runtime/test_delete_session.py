"""`sessions.delete_session` (`app/agent/runtime/sessions.py`, PRODUCTION_REVIEW.md R11, part 2): a person deletes one of their own conversations.

Ownership is checked first and is the whole authorization boundary: the checkpointer has no tenant, person or domain to scope by, so nothing may reach
`delete_thread` for an id that `session_belongs_to` did not clear."""
import pytest

from app.agent.runtime import sessions
from app.ports.outbound.checkpointing import checkpointing
from app.ports.outbound.jobs import turn_queue
from app.ports.outbound.sessions import session_store
from tests.fakes.sessions import InMemoryCheckpointing, InMemorySessionStore

ALICE = {"tenant": "acme", "principal": "alice", "claims": {}}


class _Queue:
    """Only what `delete_session` asks of the turn queue."""

    def __init__(self):
        self.busy: set[str] = set()

    async def is_thread_busy(self, thread_id: str) -> bool:
        return thread_id in self.busy


@pytest.fixture
def world():
    log: list[str] = []
    store, saver, queue = InMemorySessionStore(log), InMemoryCheckpointing(log), _Queue()
    saver.opened = True
    store.add("mine", tenant="acme", principal="alice", domain="ecorp")
    with session_store.overriding(store), checkpointing.overriding(saver), turn_queue.overriding(queue):
        yield store, saver, queue, log


async def test_the_owner_deletes_the_checkpoints_and_then_the_row(world):
    store, saver, _, log = world

    assert await sessions.delete_session(ALICE, "mine") is True

    assert "mine" not in store.rows and saver.deleted == ["mine"]
    assert log == ["checkpoints:mine", "row:mine"]


@pytest.mark.parametrize(
    ("ctx", "domain"),
    [
        ({"tenant": "acme", "principal": "mallory", "claims": {}}, "ecorp"),  # another person
        ({"tenant": "globex", "principal": "alice", "claims": {}}, "ecorp"),  # another tenant
        (ALICE, "sales"),  # another domain
    ],
    ids=["another person", "another tenant", "another domain"],
)
async def test_a_conversation_that_is_not_the_callers_reads_as_not_found_and_nothing_reaches_the_checkpointer(world, ctx, domain):
    store, saver, _, log = world

    assert await sessions.delete_session(ctx, "mine", domain) is False

    assert "mine" in store.rows and saver.deleted == [] and log == []


async def test_an_unknown_id_and_an_invalid_context_are_not_found_and_touch_nothing(world):
    store, saver, _, log = world

    assert await sessions.delete_session(ALICE, "never-existed") is False
    assert await sessions.delete_session(None, "mine") is False
    assert await sessions.delete_session({"tenant": "", "principal": "", "claims": {}}, "mine") is False

    assert "mine" in store.rows and log == []


async def test_a_conversation_with_a_turn_running_on_it_is_refused_and_nothing_is_deleted(world):
    store, saver, queue, log = world
    queue.busy.add("mine")

    with pytest.raises(sessions.SessionBusyError):
        await sessions.delete_session(ALICE, "mine")

    assert "mine" in store.rows and log == []


async def test_the_busy_check_is_for_an_owned_conversation_only_so_it_never_confirms_another_persons_thread_exists(world):
    """A caller probing someone else's id must get the same "not found" whether or not a turn is running on it."""
    _, _, queue, _ = world
    queue.busy.add("mine")
    mallory = {"tenant": "acme", "principal": "mallory", "claims": {}}

    assert await sessions.delete_session(mallory, "mine") is False


async def test_when_the_checkpoints_cannot_be_deleted_the_row_stays_so_the_owner_can_try_again(world):
    store, saver, _, _ = world
    saver.fail_for = {"mine"}

    with pytest.raises(ConnectionError):
        await sessions.delete_session(ALICE, "mine")
    assert "mine" in store.rows

    saver.fail_for = set()
    assert await sessions.delete_session(ALICE, "mine") is True


async def test_a_failure_between_the_two_deletes_leaves_a_row_the_owner_can_delete_again(world):
    store, saver, _, _ = world
    store.fail_delete = ConnectionError("appdata went away")

    with pytest.raises(ConnectionError):
        await sessions.delete_session(ALICE, "mine")
    assert "mine" in store.rows and saver.deleted == ["mine"]

    store.fail_delete = None
    assert await sessions.delete_session(ALICE, "mine") is True and "mine" not in store.rows


async def test_deleting_twice_the_second_is_not_found(world):
    await sessions.delete_session(ALICE, "mine")

    assert await sessions.delete_session(ALICE, "mine") is False


async def test_another_conversation_of_the_same_person_is_untouched(world):
    store, saver, _, _ = world
    store.add("other", tenant="acme", principal="alice", domain="ecorp")

    await sessions.delete_session(ALICE, "mine")

    assert "other" in store.rows and saver.deleted == ["mine"]
