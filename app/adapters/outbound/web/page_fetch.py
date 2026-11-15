"""The `PageFetcher` adapter (`app/ports/outbound/web.py`): one plain GET of a public URL for the ingestion pipeline (`app/ingestion/ingestor.py::ingest_url`).

Not the crawler (`app/adapters/outbound/crawl4ai/web_crawler.py` renders a page in a browser): this fetches the bytes and nothing else. `follow_redirects=False` is
what keeps a validated URL from leading to an unvalidated one.

Three things it does itself, which the caller's own SSRF check cannot (PRODUCTION_REVIEW.md R20):
  - **It connects to the address it checked.** `resolve_checked_async` resolves the host and refuses unless every address is global; the connection then goes to one
    of THOSE addresses, so a name that answers differently the second time (DNS rebinding) cannot reach an internal one. The real hostname is still the TLS server
    name (`sni_hostname`, so the certificate is verified against it) and the `Host` header.
  - **It streams the body and stops at the limit.** It used to read the whole body and compare its length to `max_bytes` afterwards, so a large response was held in
    memory before it was refused. A declared `Content-Length` over the limit is refused before a byte is read.
  - **An address that refuses the connection is followed by the next one** the host resolved to, as a browser would.
"""
import httpx

from app.core.url_safety import UnsafeURLError, resolve_checked_async
from app.ports.outbound.web import FetchedPage, PageFetchFailedError

_TIMEOUT_SECONDS = 10
_USER_AGENT = "agent-core-demo-ingestor/1.0"


class HttpxPageFetcher:
    async def fetch(self, url: str, *, max_bytes: int) -> FetchedPage:
        try:
            addresses = await resolve_checked_async(url)
        except UnsafeURLError as exc:
            raise PageFetchFailedError(f"refused: {exc}") from exc
        original = httpx.URL(url)
        last_failure: httpx.HTTPError | None = None
        for address in addresses:
            try:
                return await self._fetch_from(original, address, max_bytes)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_failure = exc  # this address did not answer: the next one the name resolved to may
            except httpx.HTTPError as exc:
                raise PageFetchFailedError(str(exc)) from exc
        raise PageFetchFailedError(str(last_failure))

    async def _fetch_from(self, original: httpx.URL, address: str, max_bytes: int) -> FetchedPage:
        """One GET over a connection to `address`, with `original`'s host as the TLS server name and the `Host` header."""
        host = original.host
        async with (
            httpx.AsyncClient(follow_redirects=False, timeout=_TIMEOUT_SECONDS) as client,
            client.stream(
                "GET",
                original.copy_with(host=address),
                headers={"User-Agent": _USER_AGENT, "Host": original.netloc.decode("ascii")},
                extensions={"sni_hostname": host},
            ) as response,
        ):
            declared = _declared_length(response)
            if declared is not None and declared > max_bytes:
                return _too_large(response, declared)
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    return _too_large(response, declared if declared is not None else total)
                chunks.append(chunk)
            return FetchedPage(
                status_code=response.status_code,
                content_type=response.headers.get("content-type", ""),
                text=b"".join(chunks).decode(response.encoding or "utf-8", errors="replace"),
                size=total,
            )


def _declared_length(response: httpx.Response) -> int | None:
    try:
        return int(response.headers["content-length"])
    except (KeyError, ValueError):
        return None


def _too_large(response: httpx.Response, size: int) -> FetchedPage:
    """The status, the type and how large it is (at least this, when it was not declared): `text` stays empty, so a caller never decodes what it will refuse."""
    return FetchedPage(status_code=response.status_code, content_type=response.headers.get("content-type", ""), text="", size=size)
