"""Document upload, its SSE progress, and document deletion.

Fire-and-forget: `POST /ingest/upload` only gets bytes into object storage and a job onto the
ingest queue, which is SEPARATE from the chat-turn queue (see app/adapters/outbound/redis/ingest_queue.py);
parsing and embedding happen in ingest_worker.py. `GET /ingest/stream/{job_id}` relays that job's
progress. `DELETE /ingest/documents` removes a document's chunks and stored files (PRODUCTION_REVIEW.md R18).
"""
import asyncio
import json
import logging
import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from app.adapters.inbound.api.deps import get_ctx
from app.adapters.inbound.api.events import EventStreamResponse, IngestEvent
from app.adapters.inbound.api.schemas import (
    IngestDeleteRequest,
    IngestDeleteResult,
    IngestUploadResult,
)
from app.core import metrics
from app.core.config import (
    INGEST_FIRST_RESPONSE_DEADLINE_SECONDS,
    MAX_UPLOAD_FILES_PER_REQUEST,
    MAX_UPLOAD_SIZE_MB,
)
from app.core.security import SecurityCtx
from app.ingestion.extractors import EXTRACTORS_BY_SUFFIX
from app.ingestion.ingestor import upload_source
from app.ports.outbound.jobs import ingest_queue
from app.ports.outbound.retrieval import document_writer
from app.ports.outbound.storage import object_store

logger = logging.getLogger(__name__)
router = APIRouter(tags=["ingest"])

_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024  # 1 MB per read() call
_MAX_UPLOAD_BYTES = MAX_UPLOAD_SIZE_MB * 1024 * 1024


def _object_key(tenant: str, job_id: str, filename: str) -> str:
    return f"{tenant}/{job_id}-{filename}"


def _is_upload_of(key: str, tenant: str, filename: str) -> bool:
    """Whether `key` is an upload of `filename` by `tenant`: exactly `_object_key`'s shape (a job id is `uuid4().hex`). Matching the whole shape, not a
    prefix or a suffix, is what keeps another tenant's `<tenant>/...` keys, or a file whose name merely ends the same way, out of a delete."""
    return re.fullmatch(rf"{re.escape(tenant)}/[0-9a-f]{{32}}-{re.escape(filename)}", key) is not None


async def _read_bounded(upload: UploadFile, filename: str) -> bytes:
    """Reads in chunks and rejects as soon as the running total crosses the
    limit — never materializes much more than one chunk past
    `_MAX_UPLOAD_BYTES`, unlike a bare `await upload.read()` which reads the
    entire file first. An unbounded upload is a memory/storage exhaustion
    vector, not just a slow request."""
    chunks = []
    total = 0
    while True:
        chunk = await upload.read(_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_UPLOAD_BYTES:
            metrics.agent_upload_rejected_total.labels(reason="too_large").inc()
            raise HTTPException(
                status_code=413,
                detail=f"{filename!r} exceeds the {MAX_UPLOAD_SIZE_MB}MB upload limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("/ingest/upload", response_model=list[IngestUploadResult])
async def ingest_upload(
    files: list[UploadFile] = File(...),
    topic: str | None = Form(None),
    ctx: SecurityCtx = Depends(get_ctx),
) -> list[IngestUploadResult]:
    """Upload documents to build this tenant's corpus — PDF/DOCX only today
    (app/ingestion/extractors.py); `.txt`/`.md` have their own path
    (ingestor.py::ingest_file, the CLI/script-driven one).

    Fire-and-forget per file: this handler only gets bytes into MinIO and a
    job onto the queue — parsing/embedding happens in ingest_worker.py, on a
    SEPARATE queue from chat turns (see ingest_queue.py). Each file's
    outcome tracks independently via its own `job_id`
    (`GET /ingest/stream/{job_id}`), so one slow/failing file never blocks
    the others — including a FAILING one: every per-file problem (bad
    extension, too-large, a MinIO/Redis error) is caught and reported back
    as that file's own `IngestUploadResult.error`, rather than raising and
    losing whatever earlier files in the SAME request already succeeded.

    Only "too many files" is rejected synchronously, before touching any
    file — a UX/abuse guard on this one call, distinct from
    INGEST_WORKER_MAX_CONCURRENCY (it limits how many jobs one submission
    creates, not how many a worker runs at once), and genuinely
    request-wide rather than per-file.

    A file that uploads to MinIO but then fails to publish its own job is
    cleaned up (`ObjectStore.delete_object`) rather than left as an
    orphaned blob nothing will ever ingest or remove.
    """
    if len(files) > MAX_UPLOAD_FILES_PER_REQUEST:
        metrics.agent_upload_rejected_total.labels(reason="too_many_files").inc()
        raise HTTPException(
            status_code=400,
            detail=f"{len(files)} files exceeds the {MAX_UPLOAD_FILES_PER_REQUEST}-file "
            "limit per upload — split into multiple submissions",
        )
    results = []
    for upload in files:
        filename = Path(upload.filename or "").name  # strip any path component a client might send
        suffix = Path(filename).suffix.lower()
        if suffix not in EXTRACTORS_BY_SUFFIX:
            metrics.agent_upload_rejected_total.labels(reason="bad_file_type").inc()
            results.append(
                IngestUploadResult(
                    filename=filename,
                    job_id=None,
                    error=f"unsupported file type {suffix!r} — only "
                    f"{sorted(EXTRACTORS_BY_SUFFIX)} are supported",
                )
            )
            continue

        try:
            data = await _read_bounded(upload, filename)
        except HTTPException as exc:
            # _read_bounded's own too-large rejection (already metriced
            # there) — a per-file problem, not a reason to abort the rest
            # of this batch.
            results.append(IngestUploadResult(filename=filename, job_id=None, error=str(exc.detail)))
            continue

        job_id = uuid.uuid4().hex
        object_key = _object_key(ctx["tenant"], job_id, filename)
        try:
            # Blocking MinIO I/O off the event loop — this IS the shared
            # SSE-serving process, so a large upload must not stall other requests.
            await asyncio.to_thread(
                object_store.get().upload_bytes,
                object_key,
                data,
                upload.content_type or "application/octet-stream",
            )
            await ingest_queue.get().publish_ingest_request(
                job_id=job_id,
                object_key=object_key,
                filename=filename,
                content_type=upload.content_type or "application/octet-stream",
                ctx=ctx,
                topic=topic,
            )
        except Exception as exc:  # noqa: BLE001 - one file's storage/queue failure must not sink the whole batch
            # Best-effort cleanup: harmless even if upload_bytes itself is
            # what failed (deleting an object that was never created is a
            # no-op under MinIO/S3 delete semantics).
            await asyncio.to_thread(object_store.get().delete_object, object_key)
            metrics.agent_upload_failed_total.labels(reason="storage_error").inc()
            logger.warning(
                # "filename" is a reserved stdlib LogRecord attribute (the
                # source file of THIS log call) — same collision
                # app/adapters/outbound/notify/team_channel.py's own docstring already notes for
                # "message"; "upload_filename" instead.
                "ingest_upload_failed",
                extra={"upload_filename": filename, "error_class": type(exc).__name__},
            )
            results.append(
                IngestUploadResult(
                    filename=filename, job_id=None, error=f"failed to queue {filename!r} for ingestion"
                )
            )
            continue

        # The line that joins this request's id to the id the ingest worker logs under.
        logger.info("ingest_job_queued", extra={"job_id": job_id, "upload_filename": filename})
        results.append(IngestUploadResult(filename=filename, job_id=job_id, error=None))
    return results


@router.get(
    "/ingest/stream/{job_id}",
    response_class=EventStreamResponse,
    responses={
        200: {
            "description": "A server-sent event stream: `data: <event>` frames until one terminal event (`done` or `error`). The schema is that of one event.",
            "model": IngestEvent,
        }
    },
)
async def ingest_stream(job_id: str) -> StreamingResponse:
    """SSE progress for one upload's job — `{"type": "started"}`, zero or
    more `{"type": "progress", "done": N, "total": M}` while
    ingest_worker.py's embedding loop runs, then one terminal event:
    `{"type": "done", "chunks": N}` or `{"type": "error", ...}`.
    Deliberately NOT ownership-checked against app/agent/runtime/sessions.py-style records
    (this pipeline keeps no job directory) — `job_id` is a `uuid4().hex`,
    unguessable in practice, same posture as the chat results streams.
    """
    async def generate() -> AsyncIterator[str]:
        try:
            async for event in ingest_queue.get().read_results(
                job_id, first_event_deadline_seconds=INGEST_FIRST_RESPONSE_DEADLINE_SECONDS
            ):
                yield f"data: {json.dumps(event)}\n\n"
        finally:
            await ingest_queue.get().delete_results_stream(job_id)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete(
    "/ingest/documents",
    response_model=IngestDeleteResult,
    responses={404: {"description": "This tenant has no document, and no stored upload, under that file name."}},
)
async def ingest_delete_document(body: IngestDeleteRequest, ctx: SecurityCtx = Depends(get_ctx)) -> IngestDeleteResult:
    """Delete an uploaded document from the tenant's knowledge base: every chunk retrieval could return, and the stored copies of the file.

    The document is named by the file name it was uploaded under. The tenant is the signed-in caller's (never a field of the request), and both what is deleted
    from the vector store and which stored files are matched are derived from it by code (Principle I, Principle III). A file name identifies a document
    within a tenant: anyone in the tenant who uploads a name replaces, and can delete, the one before it.

    Repeating the request is safe: a second one finds nothing and answers 404. A 502 means the chunks are gone but the stored files could not be listed;
    send it again. An answer already cached for up to an hour (`SEMANTIC_CACHE_TTL_SECONDS`) may still quote the document; it is not invalidated by content.
    This is a route for a signed-in person, deliberately not an agent tool.
    """
    filename = Path(body.filename).name  # the same normalisation an upload applies, so a name that was uploaded can be deleted
    if not filename:
        raise HTTPException(status_code=422, detail="filename must name a file")
    tenant = ctx["tenant"]
    chunks = await document_writer.get().delete_document(tenant=tenant, source=upload_source(filename))
    try:
        keys = await asyncio.to_thread(object_store.get().list_keys, f"{tenant}/")
    except Exception as exc:
        # The chunks are already gone: say so, and let the caller repeat the request for the files.
        logger.warning("ingest_delete_list_failed", extra={"error_class": type(exc).__name__, "chunks_removed": chunks})
        raise HTTPException(
            status_code=502,
            detail="the document's chunks were removed but its stored files could not be listed; send the request again",
        ) from exc
    mine = [key for key in keys if _is_upload_of(key, tenant, filename)]
    for key in mine:
        await asyncio.to_thread(object_store.get().delete_object, key)
    if chunks == 0 and not mine:
        raise HTTPException(status_code=404, detail=f"no document named {filename!r}")
    logger.info("ingest_document_deleted", extra={"chunks_removed": chunks, "objects_removed": len(mine)})
    return IngestDeleteResult(filename=filename, chunks_removed=chunks, objects_removed=len(mine))
