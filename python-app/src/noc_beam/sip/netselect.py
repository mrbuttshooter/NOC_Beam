"""Helpers for selecting SIP-facing local addresses."""
from __future__ import annotations

from dataclasses import dataclass
import socket

from noc_beam.config.store import AccountConfig


@dataclass(frozen=True)
class SipTarget:
    scheme: str
    host: str
    port: int
    transport: str


def parse_sip_target(value: str, default_transport: str = "udp") -> SipTarget:
    """Parse the host/port/transport portion of a SIP route target.

    Accepts full SIP URIs, bare host[:port], and route-set values such as
    ``<sip:proxy.example.com;lr>``. It intentionally ignores userinfo and
    route params because route selection only needs the next-hop socket.
    """
    target = (value or "").strip()
    if not target:
        raise ValueError("SIP target is empty")
    if target.startswith("<") and target.endswith(">"):
        target = target[1:-1].strip()

    if ":" in target and target.split(":", 1)[0].lower() in {"sip", "sips"}:
        scheme, rest = target.split(":", 1)
        scheme = scheme.lower()
    else:
        scheme = "sip"
        rest = target

    main, _, params = rest.partition(";")
    main = main.split("?", 1)[0]
    hostport = main.rsplit("@", 1)[-1].strip()
    if not hostport:
        raise ValueError("SIP target host is empty")

    default_port = 5061 if scheme == "sips" else 5060
    if hostport.startswith("["):
        end = hostport.find("]")
        if end < 0:
            raise ValueError("Invalid IPv6 SIP target")
        host = hostport[1:end]
        tail = hostport[end + 1:]
        port = int(tail[1:]) if tail.startswith(":") else default_port
    elif ":" in hostport:
        host, port_text = hostport.rsplit(":", 1)
        port = int(port_text)
    else:
        host = hostport
        port = default_port

    transport = "tls" if scheme == "sips" else (default_transport or "udp").lower()
    for param in params.split(";"):
        key, _, param_value = param.partition("=")
        if key.strip().lower() == "transport" and param_value:
            transport = param_value.strip().lower()
            break
    return SipTarget(scheme=scheme, host=host, port=port, transport=transport)


def route_target_for_account(cfg: AccountConfig) -> str:
    proxy = (getattr(cfg, "proxy", "") or "").strip()
    if proxy:
        return proxy
    host = (getattr(cfg, "domain", "") or "").strip()
    port = int(getattr(cfg, "port", 0) or 0)
    if port and host and not (host.endswith(f":{port}") or "]" in host):
        return f"{host}:{port}"
    return host


def effective_transport_for_account(cfg: AccountConfig) -> str:
    # Honor the account's configured transport (default UDP). We used to
    # force Teles accounts onto TCP here, but a matched SIP-trace pair on
    # the SAME Teles route proved that breaks call teardown: over TCP the
    # Communi5 switch can return responses on the connection but cannot
    # deliver the IN-DIALOG far-end BYE/486 to us, so a remote hangup or
    # reject never reaches NOC_Beam and the call hangs (the switch even
    # returns 481 to our later BYE -- it had already cleared the call).
    # The legacy eyeBeam tool runs over UDP on the same route and gets the
    # BYE/486 fine. So: no forced UDP->TCP coercion. An operator can still
    # pick TCP/TLS explicitly per account if a particular trunk needs it.
    return (getattr(cfg, "transport", "") or "udp").lower()


def local_address_for_sip_target(value: str, default_transport: str = "udp") -> str:
    """Return the local IP Windows would use to reach a SIP next hop.

    Returns "" on any failure (DNS miss, route unreachable, IPv6 mismatch,
    socket exhaustion, timeout). Callers MUST treat empty as "don't set
    publicAddress" — the previous version raised silently into a bare
    except, which then meant we ALSO silently lost the NAT-publicAddress
    fix that this whole module exists to provide. Now empty is explicit
    and the caller logs at WARNING with the account id.

    DNS hardening: getaddrinfo() ignores socket.settimeout(), so we
    resolve via a thread-pool with a hard 500 ms cap. Without this a
    flaky corporate resolver could freeze the Qt main thread for 5+
    seconds during account.configure() / endpoint.start().
    """
    try:
        target = parse_sip_target(value, default_transport=default_transport)
    except Exception:
        return ""
    if not target.host:
        return ""

    # Resolve host to (family, sockaddr) with a deadline. Use AF_UNSPEC
    # so v4 and v6 both work, and pick the first usable one (kernel will
    # then pick the right local interface).
    import concurrent.futures
    def _resolve():
        try:
            return socket.getaddrinfo(
                target.host, target.port,
                family=socket.AF_UNSPEC,
                type=socket.SOCK_DGRAM,
            )
        except socket.gaierror:
            return []

    addrs: list = []
    # Do NOT use the executor as a context manager here. `Executor.__exit__`
    # calls shutdown(wait=True), which JOINS the worker thread -- and that
    # worker is still blocked inside socket.getaddrinfo() (which ignores
    # socket.settimeout()). That join silently defeats the hard 500ms cap:
    # the `return ""` below would run only AFTER __exit__ waited out the
    # stuck resolver, re-freezing the Qt main thread for the full DNS
    # timeout (5+ s) -- exactly the hang this function exists to prevent.
    # Instead shutdown(wait=False): we get our 500ms deadline back, and the
    # leaked worker thread finishes (or dies with the process) on its own.
    # cancel_futures=True (Python 3.9+) drops any not-yet-started work so we
    # don't leave queued callables hanging around either.
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(_resolve)
        try:
            addrs = future.result(timeout=0.5)
        except concurrent.futures.TimeoutError:
            # DNS too slow; skip publicAddress entirely (the `finally`
            # below tears the pool down without joining the stuck worker).
            return ""
        except Exception:
            return ""
    finally:
        # Non-blocking on EVERY path: never join a resolver thread that may
        # still be stuck in getaddrinfo(). The leaked worker finishes in the
        # background (or dies with the process); that's the accepted
        # tradeoff for keeping the hard 500ms cap on the Qt main thread.
        pool.shutdown(wait=False, cancel_futures=True)

    for family, _socktype, _proto, _canon, sockaddr in addrs:
        try:
            with socket.socket(family, socket.SOCK_DGRAM) as sock:
                sock.settimeout(0.5)
                sock.connect(sockaddr)
                return str(sock.getsockname()[0])
        except OSError:
            continue
        except Exception:
            continue
    return ""


def local_address_for_account(cfg: AccountConfig) -> str:
    try:
        return local_address_for_sip_target(
            route_target_for_account(cfg),
            default_transport=effective_transport_for_account(cfg),
        )
    except Exception:
        return ""
