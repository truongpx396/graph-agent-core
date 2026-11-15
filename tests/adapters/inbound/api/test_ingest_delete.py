"""`DELETE /ingest/documents` (PRODUCTION_REVIEW.md R18): remove a document's chunks and the stored copies of the file.

The tenant is the signed-in caller's and nothing in the request can change it (Principle I); the targets are derived by code from the file name
(Principle III). The route is a person's, not an agent tool."""
import logging

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from app.adapters.inbound.api import deps
from app.adapters.inbound.api.routers import ingest as ingest_router
from app.adapters.inbound.api.schemas import IngestDeleteRequest
from app.ports.outbound.retrieval import document_writer
from app.ports.outbound.storage import object_store
from tests.conftest import TEST_CTX
from tests.fakes.documents import InMemoryDocumentWriter, InMemoryObjectStore

TENANT = TEST_CTX["tenant"]
JOB_A = "a" * 32
JOB_B = "b" * 32
SOURCE = "upload:handbook.pdf"


def _chunk(tenant=TENANT, source=SOURCE, kind="document"):
    return {"tenant": tenant, "source": source, "kind": kind, "text": "t"}


@pytest.fixture
def stores():
    writer = InMemoryDocumentWriter()
    store = InMemoryObjectStore(
        [
            f"{TENANT}/{JOB_A}-handbook.pdf",  # two uploads of the same name: both go
            f"{TENANT}/{JOB_B}-handbook.pdf",
            f"{TENANT}/{JOB_A}-other.pdf",  # another file
            f"{TENANT}/{JOB_A}-old-handbook.pdf",  # a file whose name only ENDS the same way
            f"{TENANT}/x/{JOB_A}-handbook.pdf",  # a tenant called "<tenant>/x", which the listing prefix also matches
            f"globex/{JOB_A}-handbook.pdf",  # another tenant's upload of the same name
        ]
    )
    writer.points.update(
        {
            "mine-1": _chunk(),
            "mine-2": _chunk(),
            "memory": _chunk(kind="memory"),  # same source name, but a memory is never a document
            "other-file": _chunk(source="upload:other.pdf"),
            "other-tenant": _chunk(tenant="globex"),
        }
    )
    with document_writer.overriding(writer), object_store.overriding(store):
        yield writer, store


async def _delete(filename="handbook.pdf", ctx=TEST_CTX):
    return await ingest_router.ingest_delete_document(IngestDeleteRequest(filename=filename), ctx=ctx)


async def test_it_removes_the_chunks_and_every_stored_upload_of_that_name_and_nothing_else(stores):
    writer, store = stores

    result = await _delete()

    assert (result.filename, result.chunks_removed, result.objects_removed) == ("handbook.pdf", 2, 2)
    assert set(writer.points) == {"memory", "other-file", "other-tenant"}
    assert set(store.objects) == {
        f"{TENANT}/{JOB_A}-other.pdf",
        f"{TENANT}/{JOB_A}-old-handbook.pdf",
        f"{TENANT}/x/{JOB_A}-handbook.pdf",
        f"globex/{JOB_A}-handbook.pdf",
    }


async def test_the_tenant_is_the_callers_and_a_request_cannot_name_another(stores):
    writer, store = stores

    request = IngestDeleteRequest.model_validate({"filename": "handbook.pdf", "tenant": "globex", "source": "upload:other.pdf"})
    assert not hasattr(request, "tenant") and not hasattr(request, "source"), "the request has no field a caller could aim at another tenant or source"

    await ingest_router.ingest_delete_document(request, ctx=TEST_CTX)

    assert "other-tenant" in writer.points and "other-file" in writer.points
    assert (TENANT, SOURCE, None) in writer.deletes and not any(t == "globex" for t, _, _ in writer.deletes)


async def test_another_tenant_deletes_only_its_own_document_of_that_name(stores):
    writer, store = stores

    result = await _delete(ctx={**TEST_CTX, "tenant": "globex"})

    assert (result.chunks_removed, result.objects_removed) == (1, 1)
    assert {"mine-1", "mine-2"} <= set(writer.points), "the first tenant's chunks are untouched"
    assert f"{TENANT}/{JOB_A}-handbook.pdf" in store.objects


async def test_a_directory_part_of_the_name_is_dropped_as_an_upload_drops_it(stores):
    result = await _delete("some/dir/handbook.pdf")

    assert result.filename == "handbook.pdf" and result.chunks_removed == 2


async def test_a_name_with_nothing_stored_under_it_is_a_404_and_touches_nothing(stores):
    writer, store = stores
    before_points, before_objects = dict(writer.points), dict(store.objects)

    with pytest.raises(HTTPException) as caught:
        await _delete("never-uploaded.pdf")

    assert caught.value.status_code == 404
    assert writer.points == before_points and store.objects == before_objects


async def test_repeating_the_request_is_safe_the_second_one_finds_nothing(stores):
    await _delete()

    with pytest.raises(HTTPException) as caught:
        await _delete()

    assert caught.value.status_code == 404


async def test_a_document_whose_stored_files_are_already_gone_is_still_deleted(stores):
    writer, store = stores
    store.objects.clear()

    result = await _delete()

    assert (result.chunks_removed, result.objects_removed) == (2, 0)


async def test_stored_files_with_no_chunks_left_are_still_removed_so_a_retry_after_a_partial_failure_finishes(stores):
    writer, store = stores
    store.fail_list = RuntimeError("minio is down")
    with pytest.raises(HTTPException):
        await _delete()
    store.fail_list = None

    result = await _delete()

    assert (result.chunks_removed, result.objects_removed) == (0, 2)


async def test_when_the_stored_files_cannot_be_listed_the_chunks_are_gone_and_the_answer_says_to_try_again(stores, caplog):
    writer, store = stores
    store.fail_list = RuntimeError("minio is down")

    with caplog.at_level(logging.WARNING, logger=ingest_router.logger.name), pytest.raises(HTTPException) as caught:
        await _delete()

    assert caught.value.status_code == 502 and "send the request again" in caught.value.detail
    assert not {"mine-1", "mine-2"} & set(writer.points), "the chunks, which are what retrieval serves, are already removed"
    assert any(r.message == "ingest_delete_list_failed" for r in caplog.records)
    assert "minio is down" not in caught.value.detail, "the store's own error text is not handed to the caller"


async def test_a_name_that_is_only_a_directory_separator_is_a_422(stores):
    """`Path("/").name` is empty: there is no file in it. (`dir/` is the file `dir`, exactly as an upload would store it.)"""
    with pytest.raises(HTTPException) as caught:
        await _delete("/")

    assert caught.value.status_code == 422


def test_an_empty_name_is_refused_by_the_schema():
    with pytest.raises(ValidationError):
        IngestDeleteRequest(filename="")


def test_a_name_longer_than_a_file_system_allows_is_refused_by_the_schema():
    with pytest.raises(ValidationError):
        IngestDeleteRequest(filename="a" * 256)


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        (f"acme/{JOB_A}-a.pdf", True),
        (f"acme/{JOB_A}-a.pdf.bak", False),
        (f"acme/{JOB_A}-xa.pdf", False),
        (f"acme/{JOB_A}--a.pdf", False),
        ("acme/short-a.pdf", False),
        (f"acme/x/{JOB_A}-a.pdf", False),
        (f"acme-corp/{JOB_A}-a.pdf", False),
        (f"Acme/{JOB_A}-a.pdf", False),
        (f"acme/{'A' * 32}-a.pdf", False),
    ],
)
def test_a_stored_key_is_an_upload_of_a_file_only_when_it_has_exactly_the_shape_an_upload_gives_it(key, expected):
    assert ingest_router._is_upload_of(key, "acme", "a.pdf") is expected


def test_a_file_name_is_matched_literally_not_as_a_pattern():
    assert ingest_router._is_upload_of(f"acme/{JOB_A}-a+b(1).pdf", "acme", "a+b(1).pdf")
    assert not ingest_router._is_upload_of(f"acme/{JOB_A}-aab1.pdf", "acme", "a+b(1).pdf")
    assert not ingest_router._is_upload_of(f"acmeX/{JOB_A}-a.pdf", "ac.e", "a.pdf"), "a '.' in a tenant id is a dot, not any character"


def test_the_key_an_upload_makes_is_the_key_a_delete_looks_for():
    assert ingest_router._is_upload_of(ingest_router._object_key("acme", JOB_A, "a b.pdf"), "acme", "a b.pdf")


async def test_a_delete_with_a_json_body_reaches_the_handler_over_http(stores):
    """FastAPI binds a body on DELETE (clients and proxies are what usually drop it): a minimal app with only this router, no lifespan."""
    app = FastAPI()
    app.include_router(ingest_router.router)
    app.dependency_overrides[deps.get_ctx] = lambda: TEST_CTX

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        ok = await client.request("DELETE", "/ingest/documents", json={"filename": "handbook.pdf"})
        again = await client.request("DELETE", "/ingest/documents", json={"filename": "handbook.pdf"})
        bad = await client.request("DELETE", "/ingest/documents", json={})

    assert ok.status_code == 200 and ok.json() == {"filename": "handbook.pdf", "chunks_removed": 2, "objects_removed": 2}
    assert again.status_code == 404
    assert bad.status_code == 422
