"""HTTP front process forwarding to a single controller on a private Unix socket."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request, Response
from starlette.responses import StreamingResponse

_HOP_HEADERS = {
    b"connection",
    b"keep-alive",
    b"proxy-authenticate",
    b"proxy-authorization",
    b"te",
    b"trailer",
    b"transfer-encoding",
    b"upgrade",
}


def _headers(raw: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    connection_tokens = {
        name.strip().lower()
        for key, value in raw
        if key.lower() == b"connection"
        for name in value.split(b",")
    }
    return [
        (key, value)
        for key, value in raw
        if key.lower() not in _HOP_HEADERS | connection_tokens | {b"forwarded"}
        and not key.lower().startswith(b"x-forwarded-")
    ]


def create_app(controller_socket: Path | None = None) -> FastAPI:
    socket = controller_socket or Path(os.environ.get("MGAR_H16_CONTROLLER_SOCKET", ""))
    if not socket.is_absolute():
        raise ValueError("Set MGAR_H16_CONTROLLER_SOCKET to the private controller socket")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with httpx.AsyncHTTPTransport(uds=str(socket), trust_env=False) as transport:
            app.state.controller = transport
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.api_route(
        "/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def forward(path: str, request: Request) -> Response:
        raw_path = request.scope.get("raw_path", b"/")
        query = request.scope["query_string"]
        url = httpx.URL("http://localhost").copy_with(
            raw_path=raw_path + (b"?" + query if query else b""),
        )
        headers = _headers(list(request.headers.raw))
        headers.append((b"x-forwarded-proto", request.url.scheme.encode("ascii")))
        if request.client is not None:
            headers.append((b"x-forwarded-for", request.client.host.encode("ascii")))
        transport: httpx.AsyncHTTPTransport = request.app.state.controller
        outgoing = httpx.Request(
            request.method,
            url,
            headers=headers,
            content=request.stream() if request.method not in {"GET", "HEAD"} else None,
            extensions={"timeout": {"connect": 2, "read": None, "write": 30, "pool": 5}},
        )
        try:
            upstream = await transport.handle_async_request(outgoing)
        except httpx.TransportError:
            return Response("Harness controller unavailable", status_code=503)

        async def body() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream.aiter_raw():
                    yield chunk
            finally:
                await upstream.aclose()

        response = StreamingResponse(body(), status_code=upstream.status_code)
        response.raw_headers = _headers(list(upstream.headers.raw))
        return response

    return app
