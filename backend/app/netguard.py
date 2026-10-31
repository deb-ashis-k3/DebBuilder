"""Outbound request guard (SSRF protection).

Every request a workflow, an import or a credential test makes on a user's behalf goes through here: only
http(s), and no private, loopback or link-local address unless the operator allows it
(OUTBOUND_ALLOW_PRIVATE / OUTBOUND_ALLOWED_HOSTS). Cloud metadata endpoints are refused even then. The check
runs on every request of a client, so a redirect to an internal address is refused as well.

    async with guarded_client(timeout=30, follow_redirects=True) as c:   # instead of httpx.AsyncClient(...)
        await c.get(url)
    await check_url(url)                                                 # for libraries that open their own socket
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from functools import lru_cache
from urllib.parse import urlsplit

import httpx

from .config import get_settings

# Metadata services of AWS / GCP / Azure / OpenStack / Alibaba, refused even when private networks are allowed.
_METADATA = [ipaddress.ip_network(n) for n in ("169.254.169.254/32", "169.254.170.2/32", "fd00:ec2::254/128", "100.100.100.200/32")]


class BlockedURL(ValueError):
    """The URL points somewhere workflows may not reach."""


@lru_cache(maxsize=1)
def _allowed() -> tuple[frozenset[str], tuple]:
    hosts, nets = set(), []
    for part in (get_settings().outbound_allowed_hosts or "").split(","):
        part = part.strip().lower()
        if not part:
            continue
        try:
            nets.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            hosts.add(part)
    return frozenset(hosts), tuple(nets)


def _own_host() -> str:
    return (urlsplit(get_settings().public_base_url).hostname or "").lower()


def _private(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast
            or ip.is_unspecified or ip in ipaddress.ip_network("100.64.0.0/10"))


def check_ip(ip_text: str, host: str = "") -> None:
    ip = ipaddress.ip_address(ip_text.split("%")[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if any(ip in n for n in _METADATA):
        raise BlockedURL(f"Requests to the cloud metadata address {ip} are not allowed")
    if not _private(ip):
        return
    hosts, nets = _allowed()
    if get_settings().outbound_allow_private or host in hosts or any(ip in n for n in nets):
        return
    raise BlockedURL(
        f"'{host or ip}' resolves to the private address {ip}. Requests to private, loopback and link-local "
        "addresses are blocked; an admin can allow them with OUTBOUND_ALLOW_PRIVATE or OUTBOUND_ALLOWED_HOSTS.")


async def check_url(url: str, schemes: tuple[str, ...] = ("http", "https")) -> None:
    """Raise BlockedURL unless `url` may be requested. Resolves the host and checks every address it maps to."""
    try:
        parts = urlsplit(str(url))
    except ValueError as e:
        raise BlockedURL(f"Invalid URL: {url}") from e
    if parts.scheme.lower() not in schemes:
        raise BlockedURL(f"Only {', '.join(schemes)} URLs are allowed (got '{parts.scheme or 'none'}')")
    host = (parts.hostname or "").lower()
    if not host:
        raise BlockedURL(f"URL has no host: {url}")
    if host == _own_host() and host not in ("localhost", "127.0.0.1", "::1"):
        return  # this app's own public address (calling its own webhooks)
    try:
        check_ip(host, host)  # a literal IP
        return
    except ValueError as e:
        if isinstance(e, BlockedURL):
            raise
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                                             type=socket.SOCK_STREAM)
    except OSError:
        return  # unresolvable: the request itself fails with a clear DNS error
    for info in infos:
        check_ip(info[4][0], host)


async def _on_request(request: httpx.Request) -> None:
    await check_url(str(request.url))


def guarded_client(**kwargs) -> httpx.AsyncClient:
    """httpx.AsyncClient that refuses blocked destinations on every request, redirects included."""
    hooks = kwargs.pop("event_hooks", None) or {}
    hooks = {**hooks, "request": [_on_request, *hooks.get("request", [])]}
    return httpx.AsyncClient(event_hooks=hooks, **kwargs)
