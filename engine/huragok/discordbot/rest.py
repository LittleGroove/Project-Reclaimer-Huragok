"""Discord's web API, with nothing but the standard library: everything Huragok sends to Discord goes out here, in both
modes (discord.py keeps the gateway and the interactions: client.py).

So webhook mode needs no bot and no library; the window's setup and the check read Discord the same way, from a second
window too, whether or not the bot is connected; and the start loop can post with Huragok down. Every call blocks: code
on the event loop uses asyncio.to_thread (the outbox does). A token or a webhook's URL never goes into an error's text.
"""

from __future__ import annotations

import http.client
import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from .. import __version__

API = "https://discord.com/api/v10"
TIMEOUT = 15.0
USER_AGENT = f"DiscordBot (https://github.com/LittleGroove/Project-Reclaimer-Huragok, {__version__})"
WEBHOOK_URL = re.compile(r"^https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/(\d+)/([\w-]+)/?$")
MAX_ANSWER = 5_000_000

# GET /applications/@me's flags: the switches on the developer portal's Bot page.
INTENT_FLAGS = {"message_content": (1 << 18) | (1 << 19), "members": (1 << 14) | (1 << 15),
                "presence": (1 << 12) | (1 << 13)}

# What a bot can be allowed to do, by the names features use.
PERMISSIONS = {
    "kick": 1 << 1, "ban": 1 << 2, "administrator": 1 << 3, "manage_channels": 1 << 4, "manage_guild": 1 << 5,
    "audit_log": 1 << 7, "view": 1 << 10, "send": 1 << 11, "manage_messages": 1 << 13, "embed": 1 << 14,
    "attach": 1 << 15, "history": 1 << 16, "move_members": 1 << 24, "manage_nicknames": 1 << 27,
    "manage_roles": 1 << 28, "manage_webhooks": 1 << 29, "manage_events": 1 << 33, "manage_threads": 1 << 34,
    "create_threads": 1 << 35, "send_in_threads": 1 << 38, "moderate": 1 << 40, "voice_status": 1 << 48,
}
PERMISSION_NAMES = {
    "kick": "Kick Members", "ban": "Ban Members", "administrator": "Administrator", "manage_channels": "Manage Channels",
    "manage_guild": "Manage Server", "audit_log": "View Audit Log", "view": "View Channel", "send": "Send Messages",
    "manage_messages": "Manage Messages", "embed": "Embed Links", "attach": "Attach Files",
    "history": "Read Message History", "move_members": "Move Members", "manage_nicknames": "Manage Nicknames",
    "manage_roles": "Manage Roles", "manage_webhooks": "Manage Webhooks", "manage_events": "Manage Events",
    "manage_threads": "Manage Threads", "create_threads": "Create Public Threads",
    "send_in_threads": "Send Messages in Threads", "moderate": "Timeout Members",
    "voice_status": "Set Voice Channel Status",
}


class DiscordError(Exception):
    """Discord said no, or couldn't be reached. The text is written for the host."""

    def __init__(self, text: str, *, status: int = 0, code: int = 0) -> None:
        super().__init__(text)
        self.status = status
        self.code = code


class BadToken(DiscordError):
    """401: the token isn't (or is no longer) a bot's."""


class Refused(DiscordError):
    """Discord understood and won't: asking again won't help (403, 404, a bad form)."""


class SlowDown(DiscordError):
    """429: nothing may be sent into this until retry_after is over."""

    def __init__(self, text: str, *, retry_after: float, everything: bool, status: int = 429, code: int = 0) -> None:
        super().__init__(text, status=status, code=code)
        self.retry_after = retry_after
        self.everything = everything  # the whole bot (or this PC, by address), not one channel


class Unreachable(DiscordError):
    """No answer, or Discord's own trouble (5xx): worth trying again later."""


@dataclass
class Reply:
    data: Any
    remaining: int | None = None  # requests left in this rate limit
    reset_after: float | None = None  # seconds until it fills again


def _number(text: str | None) -> float | None:
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None


def _problem(status: int, body: bytes) -> tuple[str, int]:
    """Discord's own words for a refusal, and its code."""
    try:
        data = json.loads(body)
    except ValueError:
        return f"Discord answered {status}", 0
    if not isinstance(data, dict):
        return f"Discord answered {status}", 0
    return f"{data.get('message') or 'Discord answered'} ({status})", int(data.get("code") or 0)


def send(method: str, url: str, *, token: str = "", body: Any = None, timeout: float = TIMEOUT,
         reason: str = "") -> Reply:
    """One request. Raises BadToken, Refused, SlowDown or Unreachable; never waits and never tries again (the outbox
    decides that)."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    data = None
    if token:
        headers["Authorization"] = f"Bot {token}"
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if reason:
        headers["X-Audit-Log-Reason"] = urllib.parse.quote(reason[:400], safe=" ")
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            raw = answer.read(MAX_ANSWER)
            remaining = _number(answer.headers.get("X-RateLimit-Remaining"))
            return Reply(json.loads(raw) if raw else None, int(remaining) if remaining is not None else None,
                         _number(answer.headers.get("X-RateLimit-Reset-After")))
    except urllib.error.HTTPError as error:
        raw = error.read(MAX_ANSWER)
        text, code = _problem(error.code, raw)
        if error.code == 401:
            raise BadToken(f"Discord refused the token: {text}", status=401, code=code) from None
        if error.code == 429:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {}
            parsed = parsed if isinstance(parsed, dict) else {}
            wait = _number(error.headers.get("Retry-After")) or _number(str(parsed.get("retry_after"))) or 5.0
            scope = str(error.headers.get("X-RateLimit-Scope") or "")
            everything = bool(parsed.get("global")) or scope == "global" or not parsed
            raise SlowDown(f"Discord asked Huragok to slow down for {wait:.1f} s", retry_after=wait,
                           everything=everything) from None
        if error.code >= 500:
            raise Unreachable(f"Discord is having trouble: {text}", status=error.code, code=code) from None
        raise Refused(text, status=error.code, code=code) from None
    except (urllib.error.URLError, TimeoutError, socket.timeout, http.client.HTTPException, OSError) as error:
        why = getattr(error, "reason", None) or type(error).__name__
        raise Unreachable(f"couldn't reach Discord: {why}") from None
    except ValueError:
        raise Unreachable("Discord's answer couldn't be read") from None


def call(method: str, path: str, token: str, *, body: Any = None, reason: str = "") -> Any:
    return send(method, f"{API}{path}", token=token, body=body, reason=reason).data


# Who and where ------------------------------------------------------------------------------------------------


def bot_user(token: str) -> dict[str, Any]:
    return call("GET", "/users/@me", token)


def application(token: str) -> dict[str, Any]:
    """The bot's application: its ID (for the invite), and whether the portal's switches are on (flags)."""
    return call("GET", "/applications/@me", token)


def bot_guilds(token: str) -> list[dict[str, Any]]:
    return call("GET", "/users/@me/guilds", token)


def guild(token: str, guild_id: str) -> dict[str, Any]:
    return call("GET", f"/guilds/{guild_id}", token)


def guild_roles(token: str, guild_id: str) -> list[dict[str, Any]]:
    return call("GET", f"/guilds/{guild_id}/roles", token)


def guild_channels(token: str, guild_id: str) -> list[dict[str, Any]]:
    return call("GET", f"/guilds/{guild_id}/channels", token)


def guild_member(token: str, guild_id: str, user_id: str) -> dict[str, Any]:
    return call("GET", f"/guilds/{guild_id}/members/{user_id}", token)


def invite_url(application_id: str, permissions: int, guild_id: str = "") -> str:
    query = {"client_id": application_id, "scope": "bot applications.commands", "permissions": str(permissions)}
    if guild_id:
        query.update(guild_id=guild_id, disable_guild_select="true")
    return "https://discord.com/oauth2/authorize?" + urllib.parse.urlencode(query)


# Messages and webhooks ----------------------------------------------------------------------------------------


def post_message(token: str, channel_id: str, payload: dict[str, Any]) -> Reply:
    return send("POST", f"{API}/channels/{channel_id}/messages", token=token, body=payload)


def channel_webhooks(token: str, channel_id: str) -> list[dict[str, Any]]:
    return call("GET", f"/channels/{channel_id}/webhooks", token)


def create_webhook(token: str, channel_id: str, name: str) -> dict[str, Any]:
    return call("POST", f"/channels/{channel_id}/webhooks", token, body={"name": name}, reason="Huragok posts through it")


def valid_webhook(url: str) -> bool:
    return bool(WEBHOOK_URL.match(url.strip())) or url.strip().startswith(f"{API}/webhooks/")


def _webhook(url: str) -> str:
    url = url.strip().rstrip("/")
    if not valid_webhook(url):
        raise Refused("that isn't a Discord webhook's address (it looks like https://discord.com/api/webhooks/…)")
    return url


def webhook_info(url: str) -> dict[str, Any]:
    """A webhook's name, channel and Discord: asks nothing of a token, and proves the address works."""
    return send("GET", _webhook(url)).data


def execute_webhook(url: str, payload: dict[str, Any], *, timeout: float = TIMEOUT) -> Reply:
    return send("POST", f"{_webhook(url)}?wait=true", body=payload, timeout=timeout)


__all__ = ["API", "BadToken", "DiscordError", "INTENT_FLAGS", "PERMISSIONS", "PERMISSION_NAMES", "Refused", "Reply",
           "SlowDown", "Unreachable", "application", "bot_guilds", "bot_user", "call", "channel_webhooks",
           "create_webhook", "execute_webhook", "guild", "guild_channels", "guild_member", "guild_roles", "invite_url",
           "post_message", "send", "valid_webhook", "webhook_info"]
