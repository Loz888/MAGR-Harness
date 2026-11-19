"""Bounded public HTTPS reads and document extraction for research."""

from __future__ import annotations

import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from html.parser import HTMLParser
from ipaddress import ip_address
from time import monotonic
from urllib.parse import urljoin, urlsplit

import httpx

from mgar_v3.domain import DomainError

_MAX_BYTES = 512_000
_ALLOW = {"text/html", "text/plain", "text/markdown"}
_HOSTNAME = re.compile(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z0-9-]{2,63}\Z")
_TEXT_TAGS = {
    "article",
    "aside",
    "blockquote",
    "br",
    "div",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "li",
    "main",
    "p",
    "section",
    "table",
    "td",
    "th",
    "tr",
}
_SKIP_TAGS = {"form", "noscript", "script", "style", "svg", "template"}
_DATE_KEYS = {
    "article:published_time",
    "datepublished",
    "dc.date.issued",
    "og:published_time",
}


def _invalid(code: str, message: str) -> DomainError:
    return DomainError(code, message, 422)


def normalized_uri(value: str) -> str:
    """Return canonical public-form HTTPS URI; connector checks origin and resolved address."""
    if len(value) > 2048 or any(ord(ch) < 0x21 or ch in "<>\\`" for ch in value):
        raise _invalid("RESEARCH_URI", "Invalid research URI.")
    try:
        parsed = urlsplit(value)
        url = httpx.URL(value)
        host = url.raw_host.decode("ascii")
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 443}
            or parsed.query
            or not _HOSTNAME.fullmatch(host)
        ):
            raise ValueError("Unsupported URI")
        try:
            ip_address(host)
        except ValueError:
            return str(url.copy_with(fragment=None))
    except (UnicodeError, ValueError, httpx.InvalidURL) as exc:
        raise _invalid(
            "RESEARCH_URI", "Research requires a public HTTPS hostname and path."
        ) from exc
    raise _invalid("RESEARCH_URI", "Research requires a public HTTPS hostname and path.")


def _resolve(host: str) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            str(info[4][0]) for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        )
    )


@dataclass(frozen=True)
class PageRead:
    uri: str
    retrieved_at: datetime
    media_type: str
    content: bytes


class PublicWebReader:
    """GET-only reads with authorized origins and the resolved IP fixed for the TLS request."""

    def __init__(
        self,
        allowed_hosts: frozenset[str],
        *,
        timeout_s: float = 8,
        resolve: Callable[[str], tuple[str, ...]] = _resolve,
        test_transport: httpx.BaseTransport | None = None,
    ) -> None:
        if (
            not allowed_hosts
            or not 0 < timeout_s <= 20
            or any(not _HOSTNAME.fullmatch(host.lower()) for host in allowed_hosts)
        ):
            raise ValueError("Public web reader requires bounded deployment configuration.")
        self._hosts = frozenset(
            httpx.URL(normalized_uri("https://" + host + "/")).host for host in allowed_hosts
        )
        self._resolve = resolve
        self._test_transport = test_transport
        self._timeout = timeout_s

    def authorize(self, uri: str) -> str:
        uri = normalized_uri(uri)
        if httpx.URL(uri).host not in self._hosts:
            raise DomainError("RESEARCH_SCOPE", "Research origin is outside source authority.", 403)
        return uri

    def open(
        self,
        uri: str,
        *,
        permitted_uris: frozenset[str] | None = None,
        deadline_at: datetime | None = None,
    ) -> PageRead:
        allowance = self._timeout
        if deadline_at is not None:
            allowance = min(allowance, (deadline_at - datetime.now(UTC)).total_seconds())
        deadline = monotonic() + allowance
        uri = self.authorize(uri)
        visited: set[str] = set()
        try:
            for _ in range(4):
                if uri in visited:
                    break
                if permitted_uris is not None and uri not in permitted_uris:
                    raise DomainError(
                        "RESEARCH_SCOPE", "URI is outside the task source scope.", 403
                    )
                if monotonic() >= deadline:
                    raise _invalid("RESEARCH_TIMEOUT", "Research read exceeded its time allowance.")
                visited.add(uri)
                url = httpx.URL(uri)
                addresses = tuple(ip_address(address) for address in self._resolve(url.host))
                if not addresses or any(
                    not address.is_global
                    or address.is_multicast
                    or address.is_reserved
                    or getattr(address, "ipv4_mapped", None)
                    for address in addresses
                ):
                    raise DomainError(
                        "RESEARCH_SCOPE",
                        "Research requires routable public addresses.",
                        403,
                    )
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise _invalid("RESEARCH_TIMEOUT", "Research read exceeded its time allowance.")
                address = sorted(addresses, key=lambda item: item.version)[0]
                pinned = url.copy_with(host=str(address))
                # A fresh client prevents TLS sessions or cookies from crossing origins.
                with (
                    httpx.Client(
                        transport=self._test_transport,
                        timeout=httpx.Timeout(remaining, connect=min(remaining, 4)),
                        follow_redirects=False,
                        trust_env=False,
                    ) as client,
                    client.stream(
                        "GET",
                        pinned,
                        extensions={"sni_hostname": url.host},
                        headers={
                            "Host": url.host,
                            "Accept-Encoding": "identity",
                            "Accept": "text/html,text/plain,text/markdown",
                            "User-Agent": "MAGR-Harness Research/1",
                        },
                    ) as response,
                ):
                    if response.status_code in {301, 302, 303, 307, 308}:
                        target = response.headers.get("location")
                        if not target:
                            break
                        uri = self.authorize(urljoin(uri, target))
                        continue
                    if response.status_code != 200:
                        raise _invalid("RESEARCH_HTTP", "Public source did not return status 200.")
                    media = (
                        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    )
                    length = response.headers.get("content-length")
                    if (
                        media not in _ALLOW
                        or response.headers.get("content-encoding", "identity").lower()
                        != "identity"
                        or length is not None
                        and (not length.isdigit() or int(length) > _MAX_BYTES)
                    ):
                        raise _invalid("RESEARCH_CONTENT", "Unsupported or oversized web content.")
                    body = bytearray()
                    for chunk in response.iter_raw():
                        body.extend(chunk)
                        if len(body) > _MAX_BYTES:
                            raise _invalid(
                                "RESEARCH_CONTENT", "Web content exceeds its size limit."
                            )
                        if monotonic() > deadline:
                            raise _invalid(
                                "RESEARCH_TIMEOUT",
                                "Research read exceeded its time allowance.",
                            )
                    return PageRead(uri, datetime.now(UTC), media, bytes(body))
        except httpx.TimeoutException as exc:
            raise _invalid(
                "RESEARCH_TIMEOUT", "Research read exceeded its time allowance."
            ) from exc
        except (OSError, httpx.HTTPError) as exc:
            raise _invalid("RESEARCH_FETCH", "Could not read public source.") from exc
        raise _invalid(
            "RESEARCH_REDIRECT", "Public source redirect is invalid or exceeds its limit."
        )


@dataclass(frozen=True)
class ExtractedPage:
    title: str
    text: str
    published_at: datetime | None
    links: tuple[str, ...]


class _Extractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._ignored: list[str] = []
        self._in_title = False
        self.title: list[str] = []
        self.text: list[str] = []
        self.date_values: list[str] = []
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP_TAGS:
            self._ignored.append(tag)
        if self._ignored:
            return
        values = dict(attrs)
        if tag == "title":
            self._in_title = True
        if tag in _TEXT_TAGS:
            self.text.append("\n")
        if tag == "meta" and len(self.date_values) < 16:
            key = values.get("property") or values.get("name") or values.get("itemprop") or ""
            if key.lower() in _DATE_KEYS and (value := values.get("content")):
                self.date_values.append(value)
        if tag == "time" and len(self.date_values) < 16:
            if (values.get("itemprop") or "").lower() == "datepublished" and (
                value := values.get("datetime")
            ):
                self.date_values.append(value)
        if tag == "a" and len(self.links) < 100 and (link := values.get("href")):
            self.links.append(link)

    def handle_endtag(self, tag: str) -> None:
        if self._ignored:
            if tag == self._ignored[-1]:
                self._ignored.pop()
            return
        if tag == "title":
            self._in_title = False
        if tag in _TEXT_TAGS:
            self.text.append("\n")

    def handle_data(self, text: str) -> None:
        if not self._ignored:
            (self.title if self._in_title else self.text).append(text)


def _publication(values: list[str], retrieved_at: datetime) -> datetime | None:
    found = set()
    for value in values:
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            continue
        if moment.tzinfo is None:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
                continue
            moment = moment.replace(tzinfo=UTC)
        if moment <= retrieved_at:
            found.add(moment.astimezone(UTC))
    return next(iter(found)) if len(found) == 1 else None


def extract_page(page: PageRead) -> ExtractedPage:
    if page.media_type not in _ALLOW or len(page.content) > _MAX_BYTES:
        raise _invalid("RESEARCH_CONTENT", "Unsupported or oversized web content.")
    content = page.content.decode("utf-8", errors="replace")
    if page.media_type != "text/html":
        return ExtractedPage(page.uri, content.replace("\x00", ""), None, ())
    parser = _Extractor()
    parser.feed(content)
    parser.close()
    text = "\n".join(
        normalized
        for line in "".join(parser.text).splitlines()
        if (normalized := " ".join(line.replace("\x00", "").split()))
    )
    links = []
    for link in parser.links:
        try:
            links.append(normalized_uri(urljoin(page.uri, link)))
        except (DomainError, ValueError):
            continue
    return ExtractedPage(
        " ".join("".join(parser.title).replace("\x00", "").split())[:512] or page.uri,
        text,
        _publication(parser.date_values, page.retrieved_at),
        tuple(dict.fromkeys(links)),
    )
