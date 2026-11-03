"""Discord's web API, with nothing but the standard library: everything Huragok sends to Discord goes out here, in both
modes (discord.py keeps the gateway and the interactions: client.py).

So webhook mode needs no bot and no library; the window's setup and the check read Discord the same way, from a second
window too, whether or not the bot is connected; and the start loop can post with Huragok down. Every call blocks: code
on the event loop uses asyncio.to_thread (the outbox does). A token or a webhook's URL never goes into an error's text.

Every request names the API version. The address Discord gives for a webhook doesn't, and with no version a request goes to
Discord's default, where a "slow down" is in milliseconds: read as seconds, 1.6 s became 27 minutes (plans/discord-testing.md).
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
WEBHOOK_PATH = re.compile(r"/api(?:/v\d+)?/webhooks/(\d+)/([\w-]+)/?$")
MAX_ANSWER = 5_000_000

# GET /applications/@me's flags: the switches on the developer portal's Bot page.
INTENT_FLAGS = {"message_content": (1 << 18) | (1 << 19), "members": (1 << 14) | (1 << 15),
                "presence": (1 << 12) | (1 << 13)}

# What a bot can be allowed to do, by the names features use.
DISCORD_FLAGS = {"manage_messages": 1 << 13, "kick_members": 1 << 1, "ban_members": 1 << 2, "moderate_members": 1 << 40, "manage_guild": 1 << 5, "manage_roles": 1 << 28, "administrator": 1 << 3}  # what [commands] staff_permission can name
PERMISSIONS = {
    "kick": 1 << 1, "ban": 1 << 2, "administrator": 1 << 3, "manage_channels": 1 << 4, "manage_guild": 1 << 5,
    "audit_log": 1 << 7, "view": 1 << 10, "send": 1 << 11, "manage_messages": 1 << 13, "embed": 1 << 14,
    "attach": 1 << 15, "history": 1 << 16, "move_members": 1 << 24, "change_nickname": 1 << 26, "manage_nicknames": 1 << 27,
    "manage_roles": 1 << 28, "manage_webhooks": 1 << 29, "manage_events": 1 << 33, "manage_threads": 1 << 34,
    "create_threads": 1 << 35, "create_private_threads": 1 << 36, "send_in_threads": 1 << 38, "moderate": 1 << 40, "voice_status": 1 << 48, "pin_messages": 1 << 51,
}
PERMISSION_NAMES = {
    "kick": "Kick Members", "ban": "Ban Members", "administrator": "Administrator", "manage_channels": "Manage Channels",
    "manage_guild": "Manage Server", "audit_log": "View Audit Log", "view": "View Channel", "send": "Send Messages",
    "manage_messages": "Manage Messages", "embed": "Embed Links", "attach": "Attach Files",
    "history": "Read Message History", "move_members": "Move Members", "manage_nicknames": "Manage Nicknames",
    "manage_roles": "Manage Roles", "manage_webhooks": "Manage Webhooks", "manage_events": "Manage Events",
    "manage_threads": "Manage Threads", "create_threads": "Create Public Threads", "change_nickname": "Change Nickname",
    "send_in_threads": "Send Messages in Threads", "moderate": "Timeout Members", "pin_messages": "Pin Messages", "create_private_threads": "Create Private Threads",
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
        try:
            raw = error.read(MAX_ANSWER)
        except (OSError, ValueError) as unread:  # the answer's body cut off (a timeout, a reset): tried again later,
            # not dropped as an error nobody expected (audit 2026-11, 6.5)
            raise Unreachable(f"couldn't read Discord's answer ({error.code}): {type(unread).__name__}") from None
        text, code = _problem(error.code, raw)
        if error.code == 401 and token:  # without one, it's a webhook's address that's wrong, which says nothing about the bot's token
            raise BadToken(f"Discord refused the token: {text}", status=401, code=code) from None
        if error.code == 429:
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = {}
            parsed = parsed if isinstance(parsed, dict) else {}
            # In seconds, as every request here names a version: retry_after to the millisecond, else the whole seconds
            # of Retry-After (Reset-After is when the whole allowance fills again, longer than this one post has to wait).
            wait = _number(str(parsed.get("retry_after"))) or _number(error.headers.get("Retry-After")) or 5.0
            scope = str(error.headers.get("X-RateLimit-Scope") or "")
            everything = bool(parsed.get("global")) or scope == "global" or not parsed
            raise SlowDown(f"Discord asked Huragok to slow down for {wait:.1f} s", retry_after=wait,
                           everything=everything) from None
        if error.code >= 500 or not raw.strip().startswith(b"{" if isinstance(raw, bytes) else "{"):  # every answer of Discord's own is JSON: this is what's in front of it, and says nothing about what the bot may do
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


def _webhook_parts(url: str) -> tuple[str, str] | None:
    """A webhook's ID and token, from an address on Discord (or wherever API points, in a test)."""
    url = url.strip()
    found = WEBHOOK_URL.match(url)
    if found is None and url.startswith(API.rsplit("/api", 1)[0] + "/api"):
        found = WEBHOOK_PATH.search(url)
    return (found.group(1), found.group(2)) if found else None


def valid_webhook(url: str) -> bool:
    return _webhook_parts(url) is not None


def _webhook(url: str) -> str:
    """Where to send it: this webhook on the version everything else here uses, however its address was written."""
    parts = _webhook_parts(url)
    if parts is None:
        raise Refused("that isn't a Discord webhook's address (it looks like https://discord.com/api/webhooks/…)")
    return f"{API}/webhooks/{parts[0]}/{parts[1]}"


def webhook_info(url: str) -> dict[str, Any]:
    """A webhook's name, channel and Discord: asks nothing of a token, and proves the address works."""
    return send("GET", _webhook(url)).data


def start_thread(token: str, channel_id: str, message_id: str, name: str) -> Any:
    """A thread under a message. Its ID is the message's."""
    return call("POST", f"/channels/{channel_id}/messages/{message_id}/threads", token, body={"name": name, "auto_archive_duration": 10080})


def close_thread(token: str, thread_id: str, closed: bool) -> Reply:
    """A thread locked and hidden, or brought back."""
    return send("PATCH", f"{API}/channels/{thread_id}", token=token, body={"archived": closed, "locked": closed}, reason="Huragok's ticket")


def execute_webhook(url: str, payload: dict[str, Any], *, timeout: float = TIMEOUT) -> Reply:
    return send("POST", f"{_webhook(url)}?wait=true&with_components=true", body=payload, timeout=timeout)


def edit_webhook_message(url: str, message_id: str, payload: dict[str, Any]) -> Reply:
    return send("PATCH", f"{_webhook(url)}/messages/{message_id}?with_components=true", body=payload)


def edit_message(token: str, channel_id: str, message_id: str, payload: dict[str, Any]) -> Reply:
    return send("PATCH", f"{API}/channels/{channel_id}/messages/{message_id}", token=token, body=payload)


def edit_channel(token: str, channel_id: str, fields: dict[str, Any]) -> Reply:
    """A channel's name and topic, in one change: they share Discord's limit of 2 in 10 minutes."""
    return send("PATCH", f"{API}/channels/{channel_id}", token=token, body=fields, reason="Huragok's status")


def edit_current_member(token: str, guild_id: str, **fields: Any) -> Reply:
    """The bot's own nick and bio in this Discord."""
    return send("PATCH", f"{API}/guilds/{guild_id}/members/@me", token=token, body=fields)


def set_voice_status(token: str, channel_id: str, status: str) -> Reply:
    return send("PUT", f"{API}/channels/{channel_id}/voice-status", token=token, body={"status": status})


def delete_message(token: str, channel_id: str, message_id: str) -> Reply:
    return send("DELETE", f"{API}/channels/{channel_id}/messages/{message_id}", token=token)


def send_dm(token: str, user_id: str, payload: dict[str, Any]) -> Reply:
    """A direct message: Discord's way is a channel with them first. Refused if they don't take DMs from this Discord."""
    channel = call("POST", "/users/@me/channels", token, body={"recipient_id": user_id})
    return post_message(token, str(channel["id"]), payload)


def start_private_thread(token: str, channel_id: str, name: str) -> Any:
    """Seen by whoever's brought in, and nobody else can bring anyone (invitable). A week before Discord tucks it away."""
    return call("POST", f"/channels/{channel_id}/threads", token, body={"name": name[:100], "type": 12, "invitable": False, "auto_archive_duration": 10080})


def add_thread_member(token: str, thread_id: str, user_id: str) -> Reply:
    return send("PUT", f"{API}/channels/{thread_id}/thread-members/{user_id}", token=token)


def bulk_delete(token: str, channel_id: str, message_ids: list[str]) -> Reply:
    """2 to 100 messages under two weeks old, with Manage Messages, even for the bot's own."""
    return send("POST", f"{API}/channels/{channel_id}/messages/bulk-delete", token=token, body={"messages": message_ids})


def pin_message(token: str, channel_id: str, message_id: str) -> Reply:
    return send("PUT", f"{API}/channels/{channel_id}/messages/pins/{message_id}", token=token)


def delete_webhook_message(url: str, message_id: str) -> Reply:
    return send("DELETE", f"{_webhook(url)}/messages/{message_id}")


__all__ = ["API", "BadToken", "DiscordError", "INTENT_FLAGS", "PERMISSIONS", "PERMISSION_NAMES", "Refused", "Reply",
           "SlowDown", "Unreachable", "application", "bot_guilds", "bot_user", "call", "channel_webhooks",
           "create_webhook", "delete_webhook_message", "edit_channel", "edit_current_member", "edit_message", "edit_webhook_message",
           "execute_webhook", "guild", "guild_channels", "guild_member", "guild_roles", "invite_url",
           "post_message", "send", "set_voice_status", "valid_webhook", "webhook_info"]
