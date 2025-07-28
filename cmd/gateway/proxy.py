"""HTTP CONNECT proxy server for agent egress control."""

import asyncio
import os
import re
from typing import Optional

import structlog

from . import docker_discovery

logger = structlog.get_logger()

# Proxy server configuration
PROXY_PORT = int(os.getenv("ACP_PROXY_PORT", "3128"))
PROXY_SERVER: Optional[asyncio.base_events.Server] = None

# Egress allowlist - will be populated from main
EGRESS_ALLOWLIST: dict[str, list[str]] = {}


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Bidirectional pipe between two streams."""
    try:
        while True:
            data = await reader.read(4096)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def _is_allowed(agent: str, url: str) -> bool:
    """Check if the agent is allowed to access the URL."""
    patterns = EGRESS_ALLOWLIST.get(agent, [])
    if not patterns:
        return False
    for pattern in patterns:
        # Basic glob matching (can be enhanced)
        regex = pattern.replace("*", ".*")
        if re.match(regex, url):
            return True
    return False


async def _handle_proxy(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    """Handle a single proxy connection."""
    peer = writer.get_extra_info("peername")
    peer_ip = peer[0] if peer else ""
    agent = docker_discovery.AGENT_IP_MAP.get(peer_ip)

    # Debug logging
    logger.info(
        "proxy_connection_debug",
        peer_ip=peer_ip,
        agent=agent,
        ip_map_size=len(docker_discovery.AGENT_IP_MAP),
    )

    try:
        # Read request line
        req_line_bytes = await reader.readline()
        if not req_line_bytes:
            writer.close()
            await writer.wait_closed()
            return

        req_line = req_line_bytes.decode("latin1").rstrip("\r\n")
        parts = req_line.split(" ")
        if len(parts) < 3:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return

        method, target, _ = parts

        # Read headers
        headers = {}
        while True:
            line = await reader.readline()
            if line in {b"\r\n", b""}:
                break
            k, v = line.decode("latin1").rstrip("\r\n").split(":", 1)
            headers[k.strip()] = v.strip()
            if not agent and k.lower() == "x-agent-name":
                agent = v.strip()

        # Only handle CONNECT method
        if method != "CONNECT":
            writer.write(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return

        # Validate agent identification
        if not agent:
            logger.warning("proxy_no_agent", peer_ip=peer_ip)
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return

        # Parse target host:port
        if ":" in target:
            host, port_str = target.rsplit(":", 1)
            try:
                port = int(port_str)
            except ValueError:
                port = 443
        else:
            host = target
            port = 443

        # Check allowlist
        url = f"https://{host}"
        if not _is_allowed(agent, url):
            logger.warning("proxy_blocked", agent=agent, url=url)
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return

        # Establish upstream connection
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(host, port)
        except Exception as e:
            logger.error("proxy_upstream_failed", host=host, port=port, error=str(e))
            writer.write(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            await writer.drain()
            writer.close()
            await writer.wait_closed()
            return

        # Send 200 Connection Established
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()

        # Start bidirectional pipe
        logger.info("proxy_tunnel_established", agent=agent, target=target)
        await asyncio.gather(
            _pipe(reader, upstream_writer),
            _pipe(upstream_reader, writer),
            return_exceptions=True,
        )

    except Exception as e:
        logger.error("proxy_handler_error", error=str(e))
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


async def _start_proxy_server() -> None:
    """Start the HTTP CONNECT proxy server."""
    global PROXY_SERVER
    PROXY_SERVER = await asyncio.start_server(_handle_proxy, "0.0.0.0", PROXY_PORT)
    logger.info("proxy_server_started", port=PROXY_PORT)


async def _proxy_bg() -> None:
    """Background task to run the proxy server."""
    await _start_proxy_server()
    await PROXY_SERVER.serve_forever()


def set_egress_allowlist(allowlist: dict[str, list[str]]) -> None:
    """Update the egress allowlist from configuration."""
    global EGRESS_ALLOWLIST
    EGRESS_ALLOWLIST = allowlist
