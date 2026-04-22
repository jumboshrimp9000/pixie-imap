#!/usr/bin/env python3
"""
Simple Inboxes IMAP OAuth Proxy

Accepts IMAP connections with username+password, exchanges those credentials for
a delegated Microsoft OAuth token, and proxies the IMAP session transparently.

The previous passwordless client_credentials bridge is intentionally disabled by
default because it does not verify mailbox ownership.
"""

import asyncio
import base64
import json
import logging
import os
import re
import signal
import ssl
import sys
import time
import urllib.parse
import urllib.request

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("imap-proxy")

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
config: dict = {}


def load_config():
    global config
    with open(CONFIG_PATH) as f:
        config = json.load(f)
    _token_cache.clear()
    log.info("Loaded config: %d tenant(s)", len(config.get("tenants", {})))


# ---------------------------------------------------------------------------
# Token cache: tenant_id -> (access_token, expiry_timestamp)
# ---------------------------------------------------------------------------
_token_cache: dict[str, tuple[str, float]] = {}
_token_lock = asyncio.Lock() if hasattr(asyncio, "Lock") else None


def _fetch_app_token_sync(tenant_id: str, client_id: str, client_secret: str) -> tuple[str, float]:
    """Blocking HTTP call to Microsoft token endpoint for app-only tokens."""
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    data = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": "https://outlook.office365.com/.default",
    }).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    token = resp["access_token"]
    expiry = time.time() + resp.get("expires_in", 3600)
    return token, expiry


def _fetch_password_token_sync(
    tenant_id: str,
    client_id: str,
    username: str,
    password: str,
    client_secret: str | None = None,
    scope: str | None = None,
) -> str:
    """Blocking HTTP call to Microsoft token endpoint for delegated IMAP access."""
    url = f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
    body = {
        "grant_type": "password",
        "client_id": client_id,
        "username": username,
        "password": password,
        "scope": scope or "https://outlook.office365.com/IMAP.AccessAsUser.All offline_access openid profile",
    }
    if client_secret:
        body["client_secret"] = client_secret
    data = urllib.parse.urlencode(body).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    return resp["access_token"]


async def get_app_token(tenant_id: str, client_id: str, client_secret: str) -> str:
    """Get a cached or fresh OAuth app token for a tenant."""
    global _token_lock
    if _token_lock is None:
        _token_lock = asyncio.Lock()

    async with _token_lock:
        cached = _token_cache.get(tenant_id)
        if cached and cached[1] - time.time() > 300:  # 5-min margin
            return cached[0]

        log.info("Fetching new OAuth token for tenant %s", tenant_id[:8])
        token, expiry = await asyncio.to_thread(_fetch_app_token_sync, tenant_id, client_id, client_secret)
        _token_cache[tenant_id] = (token, expiry)
        return token


async def get_password_token(
    tenant_id: str,
    client_id: str,
    username: str,
    password: str,
    client_secret: str | None = None,
    scope: str | None = None,
) -> str:
    return await asyncio.to_thread(
        _fetch_password_token_sync,
        tenant_id,
        client_id,
        username,
        password,
        client_secret,
        scope,
    )


# ---------------------------------------------------------------------------
# IMAP helpers
# ---------------------------------------------------------------------------
LOGIN_RE = re.compile(
    r"^(?P<tag>\S+)\s+LOGIN\s+(?P<args>.+)$",
    re.IGNORECASE,
)


def _tokenize_login_args(args: str) -> list[str]:
    tokens: list[str] = []
    current: list[str] = []
    in_quotes = False
    escaped = False

    for char in args.strip():
        if escaped:
            current.append(char)
            escaped = False
            continue
        if char == "\\" and in_quotes:
            escaped = True
            continue
        if char == '"':
            in_quotes = not in_quotes
            continue
        if char == " " and not in_quotes:
            if current:
                tokens.append("".join(current))
                current = []
            continue
        current.append(char)

    if current:
        tokens.append("".join(current))
    return tokens


def parse_login_credentials(args: str) -> tuple[str, str]:
    """Extract username and password from LOGIN arguments."""
    tokens = _tokenize_login_args(args)
    if len(tokens) < 2:
        raise ValueError("Malformed LOGIN command")
    return tokens[0], tokens[1]


def redact_username(username: str) -> str:
    username = username.strip()
    if "@" not in username:
        return "***"
    local_part, domain = username.split("@", 1)
    visible = local_part[:2]
    return f"{visible}***@{domain}"


def build_xoauth2(username: str, token: str) -> str:
    """Build base64-encoded XOAUTH2 SASL string."""
    sasl = f"user={username}\x01auth=Bearer {token}\x01\x01"
    return base64.b64encode(sasl.encode()).decode()


# ---------------------------------------------------------------------------
# Proxy connection handler
# ---------------------------------------------------------------------------
async def pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, label: str):
    """Relay bytes from reader to writer until EOF."""
    try:
        while True:
            data = await reader.read(8192)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def handle_client(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter):
    """Handle one IMAP client connection."""
    peer = client_writer.get_extra_info("peername")
    log.info("New connection from %s", peer)

    upstream_reader = None
    upstream_writer = None

    try:
        # Connect to Microsoft IMAP
        upstream_ctx = ssl.create_default_context()
        upstream_reader, upstream_writer = await asyncio.open_connection(
            config["upstream_host"],
            config["upstream_port"],
            ssl=upstream_ctx,
        )

        # Read and forward Microsoft's greeting
        greeting = await asyncio.wait_for(upstream_reader.readline(), timeout=15)
        client_writer.write(greeting)
        await client_writer.drain()

        # Pre-auth loop: relay commands until we see LOGIN
        while True:
            line = await asyncio.wait_for(client_reader.readline(), timeout=300)
            if not line:
                log.info("Client %s disconnected before LOGIN", peer)
                return

            line_str = line.decode("utf-8", errors="replace").rstrip("\r\n")
            match = LOGIN_RE.match(line_str)

            if not match:
                # Not a LOGIN command — forward to Microsoft and relay response
                upstream_writer.write(line)
                await upstream_writer.drain()
                # Read response lines until we get a tagged response
                tag = line_str.split()[0] if line_str.strip() else ""
                while True:
                    resp = await asyncio.wait_for(upstream_reader.readline(), timeout=15)
                    client_writer.write(resp)
                    await client_writer.drain()
                    resp_str = resp.decode("utf-8", errors="replace").rstrip("\r\n")
                    if resp_str.startswith(tag + " ") or not resp_str:
                        break
                continue

            # --- LOGIN intercepted ---
            client_tag = match.group("tag")
            try:
                username, password = parse_login_credentials(match.group("args"))
            except ValueError:
                client_writer.write(f"{client_tag} BAD malformed LOGIN command\r\n".encode())
                await client_writer.drain()
                return
            domain = username.split("@")[-1].lower() if "@" in username else ""
            redacted_username = redact_username(username)

            log.info("LOGIN from %s user=%s domain=%s", peer, redacted_username, domain)

            # Look up tenant
            tenant_cfg = config.get("tenants", {}).get(domain)
            if not tenant_cfg:
                log.warning("Unknown domain: %s", domain)
                client_writer.write(f"{client_tag} NO LOGIN Domain not configured\r\n".encode())
                await client_writer.drain()
                return

            auth_mode = str(tenant_cfg.get("auth_mode") or "password").strip().lower()
            allow_passwordless = bool(tenant_cfg.get("allow_insecure_passwordless_login"))

            # Get OAuth token
            try:
                if auth_mode == "password":
                    token = await get_password_token(
                        tenant_cfg["tenant_id"],
                        tenant_cfg["client_id"],
                        username,
                        password,
                        tenant_cfg.get("client_secret"),
                        tenant_cfg.get("scope"),
                    )
                elif auth_mode == "client_credentials" and allow_passwordless:
                    token = await get_app_token(
                        tenant_cfg["tenant_id"],
                        tenant_cfg["client_id"],
                        tenant_cfg["client_secret"],
                    )
                else:
                    log.error(
                        "Refusing insecure auth mode for domain=%s auth_mode=%s", domain, auth_mode
                    )
                    client_writer.write(f"{client_tag} NO LOGIN Authentication mode not allowed\r\n".encode())
                    await client_writer.drain()
                    return
            except Exception as e:
                if auth_mode == "password":
                    log.warning("Delegated auth failed for %s: %s", redacted_username, e)
                    client_writer.write(f"{client_tag} NO LOGIN Authentication failed\r\n".encode())
                else:
                    log.error("Token fetch failed for domain=%s: %s", domain, e)
                    client_writer.write(f"{client_tag} NO LOGIN Authentication service unavailable\r\n".encode())
                await client_writer.drain()
                return

            # Authenticate to Microsoft with XOAUTH2
            xoauth2 = build_xoauth2(username, token)
            auth_tag = "XOAUTH"
            upstream_writer.write(f"{auth_tag} AUTHENTICATE XOAUTH2 {xoauth2}\r\n".encode())
            await upstream_writer.drain()

            # Read Microsoft's response
            auth_resp = await asyncio.wait_for(upstream_reader.readline(), timeout=15)
            auth_resp_str = auth_resp.decode("utf-8", errors="replace").rstrip("\r\n")

            # Handle challenge response (+ line)
            if auth_resp_str.startswith("+"):
                # Send empty response to complete/abort the challenge
                upstream_writer.write(b"\r\n")
                await upstream_writer.drain()
                auth_resp = await asyncio.wait_for(upstream_reader.readline(), timeout=15)
                auth_resp_str = auth_resp.decode("utf-8", errors="replace").rstrip("\r\n")

            if auth_resp_str.startswith(f"{auth_tag} OK"):
                log.info("Auth SUCCESS for %s via %s", redacted_username, peer)
                client_writer.write(f"{client_tag} OK LOGIN completed\r\n".encode())
                await client_writer.drain()
            else:
                log.warning("Auth FAILED for %s: %s", redacted_username, auth_resp_str)
                client_writer.write(f"{client_tag} NO LOGIN Authentication failed\r\n".encode())
                await client_writer.drain()
                return

            # --- Authenticated: start bidirectional piping ---
            t1 = asyncio.create_task(pipe(client_reader, upstream_writer, "client->ms"))
            t2 = asyncio.create_task(pipe(upstream_reader, client_writer, "ms->client"))
            done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
            for t in pending:
                t.cancel()
            log.info("Session ended for %s via %s", redacted_username, peer)
            break

    except asyncio.TimeoutError:
        log.warning("Timeout for %s", peer)
    except Exception as e:
        log.error("Error handling %s: %s", peer, e)
    finally:
        for writer in (client_writer, upstream_writer):
            if writer:
                try:
                    writer.close()
                except Exception:
                    pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main():
    load_config()

    # SSL context for client-facing side
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(config["ssl_cert"], config["ssl_key"])

    server = await asyncio.start_server(
        handle_client,
        config["listen_host"],
        config["listen_port"],
        ssl=ctx,
    )

    # SIGHUP reloads config
    loop = asyncio.get_running_loop()
    loop.add_signal_handler(signal.SIGHUP, lambda: (load_config(), log.info("Config reloaded via SIGHUP")))

    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    log.info("IMAP OAuth Proxy listening on %s", addrs)

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Shutting down")
