"""A fake Discord, as the fake `dedicated` is a fake server: its web API and its gateway on a port of this machine, keeping
what it was sent. It runs on a thread of its own, so a test can call rest.py's blocking functions directly as well as
run the real bot (discord.py) against it. `use_fake_discord` points rest.py and discord.py at it."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

from testkit.discord_rules import command_problems, message_problems, modal_problems
from aiohttp import WSMsgType, web
from conftest import free_port

GUILD = "1000"
BOT_USER = "2000"
APPLICATION = "2001"
OWNER_USER = "3000"
STAFF_CHANNEL = "4001"
PUBLIC_CHANNEL = "4002"
ADMIN_ROLE = "5001"
MOD_ROLE = "5002"
BOT_ROLE = "5009"
DISCORD_EPOCH_MS = 1420070400000
TOKEN = "fake.bot.token"
REGISTRATIONS_AT_ONCE, REGISTRATION_SECONDS = 5, 30.0  # measured on the real thing, 31 October
ALL_BUT_ADMIN = str((1 << 48) | (1 << 40) | (1 << 29) | (1 << 28) | (1 << 26) | (1 << 16) | (1 << 15) | (1 << 14) | (1 << 11) | (1 << 10) | (1 << 4))


def _json(data: Any, *, status: int = 200, headers: dict[str, str] | None = None) -> web.Response:
    """As Discord sends it: discord.py reads an answer as JSON only when the type is exactly this."""
    return web.Response(body=json.dumps(data).encode("utf-8"), status=status, headers={"Content-Type": "application/json", **(headers or {})})


def _stamp() -> str:
    return "2026-10-30T00:00:00+00:00"


def _user(user_id: str, name: str, *, bot: bool = False) -> dict[str, Any]:
    return {"id": user_id, "username": name, "discriminator": "0", "avatar": None, "global_name": name, "bot": bot,
            "public_flags": 0, "flags": 0}


def id_time(snowflake: str) -> float:
    return ((int(snowflake) >> 22) + DISCORD_EPOCH_MS) / 1000


class FakeDiscord:
    def __init__(self) -> None:
        self.base = ""
        self.ws_url = ""
        self.token = TOKEN
        self.flags = (1 << 18) | (1 << 14)  # the portal's Message Content and Server Members switches
        self.bot_public = False
        self.in_guilds = [GUILD]
        self.guild = {"id": GUILD, "name": "Test Discord", "owner_id": OWNER_USER, "mfa_level": 0, "features": [],
                      "icon": None, "splash": None, "discovery_splash": None, "afk_channel_id": None, "afk_timeout": 300,
                      "verification_level": 0, "default_message_notifications": 0, "explicit_content_filter": 0,
                      "system_channel_id": None, "system_channel_flags": 0, "rules_channel_id": None, "vanity_url_code": None,
                      "description": None, "banner": None, "premium_tier": 0, "preferred_locale": "en-US",
                      "public_updates_channel_id": None, "nsfw_level": 0, "premium_progress_bar_enabled": False,
                      "emojis": [], "stickers": []}
        self.roles = [
            self._role(GUILD, "@everyone", 0, str((1 << 10) | (1 << 11))),
            self._role(ADMIN_ROLE, "SPR-Admin", 3, "0"),
            self._role(MOD_ROLE, "SPR-Mod", 2, "0"),
            self._role(BOT_ROLE, "Huragok", 4, ALL_BUT_ADMIN, managed=True),
        ]
        self.channels = [
            self._channel(STAFF_CHANNEL, "admin-log", overwrites=[{"id": GUILD, "type": 0, "allow": "0", "deny": str(1 << 10)},
                                                                  {"id": BOT_ROLE, "type": 0, "allow": str(1 << 10), "deny": "0"}]),
            self._channel(PUBLIC_CHANNEL, "servers"),
        ]
        self.webhooks: dict[str, dict[str, Any]] = {}
        self.requests: list[tuple[str, str]] = []  # (method, path) of every web request
        self.unversioned = 0  # requests that named no API version
        self.edits: list[dict[str, Any]] = []  # messages edited: the form sent, and "message"
        self.channel_edits: list[dict[str, Any]] = []
        self.member_edits: list[dict[str, Any]] = []
        self.voice: list[dict[str, Any]] = []
        self.presences: list[dict[str, Any]] = []
        self.posted: list[dict[str, Any]] = []  # what was posted: "channel", "via" ("webhook" or "bot") and the form sent
        self.commands: dict[str, list[dict[str, Any]]] = {}  # Discord -> what's registered
        self.registrations = 0
        self.callbacks: list[dict[str, Any]] = []  # answers to interactions (the form, plus "token")
        self.original_edits: list[dict[str, Any]] = []  # an interaction's card edited (the form, plus "token")
        self.threads: dict[str, str] = {}  # message -> the name of the thread under it
        self.threads_refused = False
        self.pins: list[str] = []
        self.answers_deleted: list[tuple[str, str]] = []
        self.typed: list[dict[str, Any]] = []  # what people said in channels: read back, and deleted, as Discord's are
        self.message_reads: list[str] = []
        self.threads_deleted: list[str] = []
        self.refused: list[str] = []  # what Discord wouldn't have taken: the test fails on it when the fake stops
        self.refusals_expected = False
        self.interaction_seconds = 10.0  # Discord's is 3: this much for a busy machine, and a test of it says 3
        self.interactions_at: dict[str, float] = {}
        self.answered: set[str] = set()
        self.registration_tokens, self.registration_time = float(REGISTRATIONS_AT_ONCE), time.monotonic()
        self.command_reads = 0
        self.private_threads: dict[str, dict[str, Any]] = {}
        self.dms_closed: set[str] = set()
        self.attachments: dict[str, str] = {}
        self.deleted: list[str] = []
        self.followups: list[dict[str, Any]] = []
        self.identifies: list[dict[str, Any]] = []
        self.close_with: int | None = None  # the gateway refuses with this close code
        self.refuse_next: list[tuple[str, int, Any, dict[str, str]]] = []  # (path part, status, body, headers)
        self.ready = threading.Event()
        self._ids = iter(range(900000, 999999999))
        self._sockets: list[web.WebSocketResponse] = []
        self._panels: dict[str, str] = {}  # the slash command's interaction -> its private message, as long as it's open
        self._loop = asyncio.new_event_loop()
        self._runner: web.AppRunner | None = None
        self._thread = threading.Thread(target=self._run, daemon=True, name="fake discord")

    @staticmethod
    def _role(role_id: str, name: str, position: int, permissions: str, *, managed: bool = False) -> dict[str, Any]:
        return {"id": role_id, "name": name, "position": position, "permissions": permissions, "managed": managed,
                "color": 0, "hoist": False, "mentionable": False, "flags": 0, "tags": {}}

    def _channel(self, channel_id: str, name: str, *, kind: int = 0, overwrites: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        return {"id": channel_id, "name": name, "type": kind, "guild_id": GUILD, "position": 0,
                "permission_overwrites": overwrites or [], "parent_id": None, "nsfw": False, "topic": None,
                "rate_limit_per_user": 0, "last_message_id": None, "flags": 0}

    # Starting and stopping -----------------------------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()
        if not self.ready.wait(15):
            raise RuntimeError("the fake Discord didn't start")

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())
        self.ready.set()
        self._loop.run_forever()

    async def _serve(self) -> None:
        app = web.Application()
        app.router.add_route("*", "/gateway", self._gateway)
        app.router.add_route("*", "/api/v10/{tail:.*}", self._api)
        app.router.add_route("*", "/api/webhooks/{tail:.*}", self._unversioned)
        app.router.add_route("GET", "/cdn/{name}", self._cdn)
        self._runner = web.AppRunner(app, handle_signals=False)
        await self._runner.setup()
        port = free_port()
        site = web.TCPSite(self._runner, "127.0.0.1", port)
        await site.start()
        self.base = f"http://127.0.0.1:{port}/api/v10"
        self.ws_url = f"ws://127.0.0.1:{port}/gateway"

    def _refuse(self, status: int, code: int, message: str, why: str) -> web.Response:
        """What Discord would have refused: as it does, and the test fails when the fake stops, even if the code carried on."""
        self.refused.append(why)
        return _json({"message": message, "code": code}, status=status)

    def _refuse_form(self, problems: list[str]) -> web.Response | None:
        return self._refuse(400, 50035, "Invalid Form Body", "; ".join(problems)) if problems else None

    def stop(self) -> None:
        refused, self.refused = self.refused, []
        self._stop_serving()
        if refused and not self.refusals_expected:
            raise AssertionError("Discord would have refused: " + " | ".join(refused))

    def _stop_serving(self) -> None:
        async def down() -> None:
            for socket in self._sockets:
                await socket.close()
            if self._runner is not None:
                await self._runner.cleanup()

        asyncio.run_coroutine_threadsafe(down(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)

    def next_id(self) -> str:
        """As Discord's: they say when, and later ones are always higher."""
        return str(((int(time.time() * 1000) - DISCORD_EPOCH_MS) << 22) + (next(self._ids) & 0x3FFFFF))

    # The web API ---------------------------------------------------------------------------------------------

    async def _cdn(self, request: web.Request) -> web.Response:
        """A file someone attached to a command."""
        return web.Response(body=self.attachments[request.match_info["name"]].encode("utf-8"))

    async def _unversioned(self, request: web.Request) -> web.Response:
        """A webhook's address as Discord gives it: no version, so its default one, whose waits are in milliseconds (seen on
        the real thing, 30 October: 12 answers of 1628 to 1648 for what's about 1.6 s). Nothing of Huragok's should come here."""
        self.unversioned += 1
        return _json({"message": "You are being rate limited.", "retry_after": 1648, "global": False}, status=429,
                     headers={"Retry-After": "1648"})

    def slow_down(self, part: str = "/webhooks/", retry_after: float = 0.387, *, scope: str = "user") -> None:
        """The next request whose path has `part` is told to wait, word for word as the real Discord does on v10."""
        self.refuse_next.append((part, 429, {"message": "You are being rate limited.", "retry_after": retry_after, "global": False},
                                 {"Retry-After": str(int(retry_after) + 2), "X-RateLimit-Limit": "5", "X-RateLimit-Remaining": "0",
                                  "X-RateLimit-Reset-After": "1.987", "X-RateLimit-Scope": scope, "X-RateLimit-Bucket": "abc"}))

    async def _api(self, request: web.Request) -> web.Response:
        path = "/" + request.match_info["tail"]
        self.requests.append((request.method, path))
        for entry in list(self.refuse_next):
            part, status, body, headers = entry
            if part in path:
                self.refuse_next.remove(entry)
                return _json(body, status=status, headers=headers)
        body = await request.json() if request.can_read_body and request.content_type == "application/json" else None
        if request.content_type == "multipart/form-data":  # a message with a file: its JSON, and what's in the files as text
            form = await request.post()
            body = json.loads(str(form["payload_json"]))
            body["files"] = [part.file.read().decode("utf-8") for key, part in form.items() if key != "payload_json"]
        webhook_path = path.startswith("/webhooks/") or path.startswith("/interactions/")
        if not webhook_path and request.headers.get("Authorization") != f"Bot {self.token}":
            return _json({"message": "401: Unauthorized", "code": 0}, status=401)
        return self._route(request.method, path, body, request)

    def _route(self, method: str, path: str, body: Any, request: web.Request) -> web.Response:
        parts = [part for part in path.split("/") if part]
        match (method, parts):
            case ("GET", ["users", "@me"]):
                return _json(_user(BOT_USER, "Huragok Test", bot=True))
            case ("GET", ["users", "@me", "guilds"]):
                return _json([{"id": g, "name": self.guild["name"], "icon": None, "owner": False,
                                           "permissions": ALL_BUT_ADMIN, "features": []} for g in self.in_guilds])
            case ("GET", ["applications", "@me"]) | ("GET", ["oauth2", "applications", "@me"]):
                return _json({"id": APPLICATION, "name": "Huragok Test", "icon": None, "description": "",
                                          "rpc_origins": [], "bot_public": self.bot_public, "bot_require_code_grant": False,
                                          "owner": _user(OWNER_USER, "Host"), "team": None, "summary": "", "verify_key": "0",
                                          "flags": self.flags})
            case ("POST", ["users", "@me", "channels"]):
                if body["recipient_id"] in self.dms_closed:
                    return _json({"message": "Cannot send messages to this user", "code": 50007}, status=403)
                return _json({"id": f"dm{body['recipient_id']}", "type": 1})
            case ("GET", ["gateway", "bot"]):
                return _json({"url": self.ws_url, "shards": 1,
                                          "session_start_limit": {"total": 1000, "remaining": 999, "reset_after": 1000,
                                                                  "max_concurrency": 1}})
            case ("GET", ["guilds", guild]) if guild in self.in_guilds:
                return _json({**self.guild, "roles": self.roles})
            case ("GET", ["guilds", guild, "roles"]) if guild in self.in_guilds:
                return _json(self.roles)
            case ("GET", ["guilds", guild, "channels"]) if guild in self.in_guilds:
                return _json(self.channels)
            case ("GET", ["guilds", guild, "members", user]) if guild in self.in_guilds:
                roles = [BOT_ROLE] if user == BOT_USER else []
                return _json({"user": _user(user, "Someone", bot=user == BOT_USER), "roles": roles, "nick": None,
                                          "joined_at": _stamp(), "deaf": False, "mute": False, "flags": 0})
            case ("GET", ["applications", _, "guilds", guild, "commands"]):
                self.command_reads += 1
                return _json(self.commands.get(guild, []))
            case ("PUT", ["applications", _, "guilds", guild, "commands"]):
                if refused := self._refuse_form(command_problems(body)):
                    return refused
                if (waiting := self._take_a_registration()) is not None:
                    return waiting
                self.registrations += 1
                registered = [{"description": "", **command, "id": self.next_id(), "application_id": APPLICATION, "guild_id": guild, "version": "1"}  # a right-click's has none sent, and "" back
                              for command in body]
                self.commands[guild] = registered
                return _json(registered)
            case ("GET", ["channels", channel, "webhooks"]):
                return _json([hook for hook in self.webhooks.values() if hook["channel_id"] == channel])
            case ("POST", ["channels", channel, "webhooks"]):
                hook_id = self.next_id()
                hook = {"id": hook_id, "type": 1, "name": body["name"], "channel_id": channel, "guild_id": GUILD,
                        "token": f"hook{hook_id}",
                        "url": f"{self.base.removesuffix('/v10')}/webhooks/{hook_id}/hook{hook_id}"}  # as Discord: no version
                self.webhooks[hook_id] = hook
                return _json(hook)
            case ("POST", ["channels", channel, "messages"]):
                return self._message(channel, "bot", body)
            case ("POST", ["channels", channel, "threads"]):
                if self.threads_refused:
                    return _json({"message": "Missing Permissions", "code": 50013}, status=403)
                thread = self.next_id()
                self.private_threads[thread] = {**body, "parent": channel, "members": []}
                return _json({**self._channel(thread, str(body["name"])), "type": 12, "parent_id": channel})
            case ("PUT", ["channels", thread, "thread-members", user]):
                self.private_threads[thread]["members"].append(user)
                return web.Response(status=204)
            case ("GET", ["channels", channel, "messages"]):
                self.message_reads.append(channel)
                before, after = request.query.get("before"), request.query.get("after")
                found = sorted([post for post in [*self.posted, *self.typed] if post["channel"] == channel and (not before or int(post["id"]) < int(before)) and (not after or int(post["id"]) > int(after))], key=lambda post: int(post["id"]))
                if after:  # as Discord: the oldest after that point, not the newest
                    found = found[:int(request.query.get("limit", 50))]
                return _json([{**self._message_form(channel, post), "id": post["id"], "author": post.get("author") or _user(BOT_USER, "Huragok Test", bot=True), "pinned": post["id"] in self.pins, "type": post.get("type", 0),
                                         **({"thread": post["thread"]} if post.get("thread") else {}), **({"referenced_message": {"author": _user(BOT_USER, "x")}} if post.get("message_reference") else {})} for post in reversed(found)][:int(request.query.get("limit", 50))])
            case ("GET", ["channels", channel]) if channel in self.private_threads:
                last = [post["id"] for post in self.posted if post["channel"] == channel]
                return _json({**self._channel(channel, "thread"), "type": 12, "last_message_id": last[-1] if last else self.private_threads[channel].get("last")})
            case ("GET", ["channels", channel, "messages", message]):
                return self._read_back(channel, message)
            case ("GET", ["channels", channel]):
                said = [post["id"] for post in [*self.posted, *self.typed] if post["channel"] == channel]
                known = next((one for one in self.channels if one["id"] == channel), None) or self._channel(channel, "c")
                return _json({**known, "last_message_id": max(said, key=int) if said else None})
            case ("DELETE", ["channels", thread]):
                self.threads_deleted.append(thread)
                return web.Response(status=204)
            case ("PATCH", ["webhooks", hook, token, "messages", message]) if self.webhooks.get(hook, {}).get("token") == token:
                return self._edit(message, body, hook=hook)
            case ("PATCH", ["channels", channel, "messages", message]):
                return self._edit(message, body)
            case ("PATCH", ["channels", channel]):
                self.channel_edits.append({**body, "channel": channel})
                return _json(self._channel(channel, body.get("name", "c")))
            case ("PATCH", ["guilds", _, "members", "@me"]):
                self.member_edits.append(body)
                return _json({"user": _user(BOT_USER, "Huragok Test", bot=True), "roles": [BOT_ROLE], "joined_at": _stamp(), "deaf": False, "mute": False, "flags": 0})
            case ("PUT", ["channels", channel, "voice-status"]):
                self.voice.append({"channel": channel, **body})
                return web.Response(status=204)
            case ("DELETE", ["webhooks", hook, token, "messages", message]) if self.webhooks.get(hook, {}).get("token") == token:
                if next((post for post in self.posted if post.get("id") == message and post.get("hook") == hook), None) is None:
                    return _json({"message": "Unknown Message", "code": 10008}, status=404)
                self.posted[:] = [post for post in self.posted if post.get("id") != message]
                return web.Response(status=204)
            case ("GET", ["webhooks", hook, token]) if self.webhooks.get(hook, {}).get("token") == token:
                return _json(self.webhooks[hook])
            case ("POST", ["webhooks", hook, token]) if self.webhooks.get(hook, {}).get("token") == token:
                return self._message(self.webhooks[hook]["channel_id"], "webhook", body, hook)
            case ("POST", ["webhooks", application, token]) if application == APPLICATION:  # an interaction's follow-up
                if token not in self.answered:
                    return self._refuse(404, 10015, "Unknown Webhook", "a follow-up to an interaction nothing had answered")
                if refused := self._refuse_form(message_problems(body or {}, may_be_ephemeral=True)):
                    return refused
                self.followups.append({**(body or {}), "token": token})
                return _json(self._message_form(PUBLIC_CHANNEL, body or {}))
            case ("DELETE", ["webhooks", _, token, "messages", gone]):  # an answer going from someone's screen
                self.answers_deleted.append((token, gone))
                return web.Response(status=204)
            case ("POST", ["channels", channel, "messages", "bulk-delete"]):
                messages = (body or {}).get("messages") or []
                if not 2 <= len(messages) <= 100:
                    return self._refuse(400, 50035, "Invalid Form Body", f"a bulk delete of {len(messages)} messages: it must be 2 to 100")
                if not int(next(role for role in self.roles if role["id"] == BOT_ROLE)["permissions"]) & (1 << 13):
                    return _json({"message": "Missing Permissions", "code": 50013}, status=403)
                if any(time.time() - id_time(str(message)) > 14 * 86400 for message in messages):
                    return self._refuse(400, 50034, "You can only bulk delete messages that are under 14 days old.", "a bulk delete with something over 14 days old in it")
                self.posted[:] = [post for post in self.posted if post.get("id") not in messages]
                self.typed[:] = [post for post in self.typed if post.get("id") not in messages]
                self.deleted += [str(message) for message in messages]
                return web.Response(status=204)
            case ("GET" | "PATCH", ["webhooks", _, token, "messages", "@original"]):
                if token not in self.answered:
                    return self._refuse(404, 10015, "Unknown Webhook", "the answer to an interaction nothing had answered")
                if method == "PATCH":
                    if refused := self._refuse_form(message_problems(body or {}, editing=True, may_be_ephemeral=True)):
                        return refused
                    self.original_edits.append({**(body or {}), "token": token})
                return _json(self._message_form(PUBLIC_CHANNEL, body or {}))
            case ("PUT", ["channels", _, "messages", "pins", message]):
                self.pins.append(message)
                return web.Response(status=204)
            case ("DELETE", ["channels", _, "messages", message]):
                written = next((post for post in self.posted if post.get("id") == message), None)
                if written is not None and written.get("hook") and not int(next(role for role in self.roles if role["id"] == BOT_ROLE)["permissions"]) & (1 << 13):
                    return _json({"message": "Missing Permissions", "code": 50013}, status=403)  # someone else's, as far as the bot goes
                self.posted[:] = [post for post in self.posted if post.get("id") != message]
                self.typed[:] = [post for post in self.typed if post.get("id") != message]
                self.deleted.append(message)
                return web.Response(status=204)
            case ("POST", ["channels", channel, "messages", message, "threads"]):
                if self.threads_refused:
                    return _json({"message": "Missing Permissions", "code": 50013}, status=403)
                if message in self.threads:
                    return _json({"message": "A thread has already been created for this message", "code": 160004}, status=400)
                self.threads[message] = str((body or {}).get("name"))
                return _json({**self._channel(message, str((body or {}).get("name"))), "type": 11, "parent_id": channel, "owner_id": BOT_USER, "guild_id": GUILD, "message_count": 0, "member_count": 1, "rate_limit_per_user": 0,
                              "thread_metadata": {"archived": False, "auto_archive_duration": 1440, "archive_timestamp": _stamp(), "locked": False}})
            case ("POST", ["interactions", interaction, token, "callback"]):
                if refused := self._answered(token):
                    return refused
                kind, data = (body or {}).get("type"), (body or {}).get("data") or {}
                problems = modal_problems(data) if kind == 9 else message_problems(data, editing=kind == 7, may_be_ephemeral=True) if kind == 4 or kind == 7 else []
                if refused := self._refuse_form(problems):
                    return refused
                self.callbacks.append({**(body or {}), "token": token})
                data = (body or {}).get("data") or {}
                message = self._panels.setdefault(interaction, self.next_id())
                return _json({
                    "interaction": {"id": interaction, "type": 2, "response_message_id": message,
                                    "response_message_loading": False, "response_message_ephemeral": True},
                    "resource": {"type": (body or {}).get("type", 4),
                                 "message": {**self._message_form(PUBLIC_CHANNEL, data), "id": message}},
                })
        return _json({"message": f"404: Not Found ({method} {path})", "code": 0}, status=404)

    def _message_form(self, channel: str, body: dict[str, Any]) -> dict[str, Any]:
        return {"id": self.next_id(), "channel_id": channel, "author": _user(BOT_USER, "Huragok Test", bot=True),
                "content": body.get("content", ""), "timestamp": _stamp(), "edited_timestamp": None, "tts": False,
                "mention_everyone": False, "mentions": [], "mention_roles": [], "attachments": [], "embeds": body.get("embeds", []),
                "pinned": False, "type": 0, "flags": body.get("flags", 0), "components": body.get("components", [])}

    def _take_a_registration(self) -> web.Response | None:
        """As the real thing did on 31 October: five overwrites at once, then about one every 30 seconds."""
        now = time.monotonic()
        self.registration_tokens = min(float(REGISTRATIONS_AT_ONCE), self.registration_tokens + (now - self.registration_time) / REGISTRATION_SECONDS)
        self.registration_time = now
        if self.registration_tokens >= 1:
            self.registration_tokens -= 1
            return None
        wait = (1 - self.registration_tokens) * REGISTRATION_SECONDS
        self.refused.append(f"registered the slash commands too often: told to wait {wait:.0f} s")
        return _json({"message": "You are being rate limited.", "retry_after": wait, "global": False}, status=429, headers={"Retry-After": str(int(wait) + 1), "X-RateLimit-Scope": "user"})

    def _answered(self, token: str) -> web.Response | None:
        """An answer to an interaction: within its time, and only once."""
        age = time.monotonic() - self.interactions_at.get(token, time.monotonic())
        if age > self.interaction_seconds:
            return self._refuse(404, 10062, "Unknown interaction", f"answered an interaction {age:.1f} s after it came: Discord allows 3")
        if token in self.answered:
            return self._refuse(400, 40060, "Interaction has already been acknowledged.", "answered an interaction twice")
        self.answered.add(token)
        return None

    def _message(self, channel: str, via: str, body: dict[str, Any], hook: str = "") -> web.Response:
        if refused := self._refuse_form(message_problems(body)):
            return refused
        form = self._message_form(channel, body)
        self.posted.append({**body, "channel": channel, "via": via, "id": form["id"], "hook": hook})
        return _json(form, headers={"X-RateLimit-Remaining": "4", "X-RateLimit-Reset-After": "1"})

    def _edit(self, message: str, body: dict[str, Any], *, hook: str = "") -> web.Response:
        if refused := self._refuse_form(message_problems(body, editing=True)):
            return refused
        post = next((p for p in self.posted if p.get("id") == message), None)
        if post is None:
            return _json({"message": "Unknown Message", "code": 10008}, status=404)
        if post.get("hook") != hook:  # as Discord: only whoever posted it can edit it, and a webhook is told there's no such message
            return _json({"message": "Unknown Message", "code": 10008}, status=404) if hook else _json({"message": "Cannot edit a message authored by another user", "code": 50005}, status=403)
        self.edits.append({**body, "message": message})
        return _json({**self._message_form(post["channel"], body), "id": message})

    def _read_back(self, channel: str, message: str) -> web.Response:
        """A post as the bot reads it again. Without Message Content, what came through a webhook has no text, its own or not."""
        post = next((p for p in self.posted if p.get("id") == message and p["channel"] == channel), None)
        if post is None:
            return _json({"message": "Unknown Message", "code": 10008}, status=404)
        form = {**self._message_form(channel, post), "id": message, "author": _user(BOT_USER, post.get("username", "Huragok Test")),
                "webhook_id": "1" if post["via"] == "webhook" else None}
        if post["via"] == "webhook" and not self.flags & (1 << 18):
            form.update(content="", embeds=[], attachments=[], components=[])
        return _json(form)

    # The gateway ---------------------------------------------------------------------------------------------

    async def _gateway(self, request: web.Request) -> web.WebSocketResponse:
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self._sockets.append(socket)
        await socket.send_json({"op": 10, "d": {"heartbeat_interval": 45000}})
        sequence = 0
        async for frame in socket:
            if frame.type != WSMsgType.TEXT:
                continue
            data = json.loads(frame.data)
            if data["op"] == 1:
                await socket.send_json({"op": 11})
            elif data["op"] == 3:
                self.presences.append(data["d"])
            elif data["op"] == 2:
                self.identifies.append(data["d"])
                if self.close_with is not None:
                    await socket.close(code=self.close_with)
                    break
                sequence += 1
                await socket.send_json({"op": 0, "t": "READY", "s": sequence, "d": {
                    "v": 10, "user": _user(BOT_USER, "Huragok Test", bot=True), "session_id": "fake-session",
                    "resume_gateway_url": self.ws_url, "application": {"id": APPLICATION, "flags": self.flags},
                    "guilds": [{"id": g, "unavailable": True} for g in self.in_guilds]}})
                for guild in self.in_guilds:
                    sequence += 1
                    await socket.send_json({"op": 0, "t": "GUILD_CREATE", "s": sequence, "d": {
                        **self.guild, "id": guild, "roles": self.roles, "channels": self.channels, "members": [], "threads": [],
                        "presences": [], "voice_states": [], "stage_instances": [], "guild_scheduled_events": [],
                        "soundboard_sounds": [], "member_count": 3, "large": False, "unavailable": False,
                        "joined_at": _stamp()}})
        return socket

    def dispatch(self, name: str, data: dict[str, Any]) -> None:
        """An event from Discord to the connected bot."""
        async def send() -> None:
            for socket in self._sockets:
                if not socket.closed:
                    await socket.send_json({"op": 0, "t": name, "s": int(time.time()), "d": data})

        asyncio.run_coroutine_threadsafe(send(), self._loop).result(10)

    def right_click(self, name: str, target: dict[str, Any], *, user: str = OWNER_USER, roles: tuple[str, ...] = ()) -> tuple[str, str]:
        """Someone right-clicking a message, and picking one of the bot's commands."""
        return self._interaction(2, {"id": self.next_id(), "name": name, "type": 3, "target_id": target["id"], "resolved": {"messages": {target["id"]: target}}}, user, roles, GUILD, target["channel_id"])

    def message_someone_wrote(self, channel: str, text: str, *, user: str = "7001") -> dict[str, Any]:
        return {**self._message_form(channel, {"content": text}), "author": _user(user, f"user{user}"), "guild_id": GUILD}

    def message(self, channel: str, text: str, *, user: str = OWNER_USER, roles: tuple[str, ...] = (), bot: bool = False, webhook: bool = False) -> str:
        """Someone typing in a channel: the message's ID. As Discord: nothing at all for a bot that didn't ask for messages, and no text without Message Content."""
        message = self.next_id()
        intents = int(self.identifies[-1]["intents"])
        if intents & (1 << 9):
            self.dispatch("MESSAGE_CREATE", {"id": message, "channel_id": channel, "guild_id": GUILD, "author": _user(user, f"user{user}", bot=bot), "member": {k: v for k, v in self.member(user, roles=roles).items() if k != "user"},
                                             "content": text if intents & (1 << 15) else "", "timestamp": _stamp(), "edited_timestamp": None, "tts": False, "mention_everyone": False, "mentions": [], "mention_roles": [],
                                             "attachments": [], "embeds": [], "pinned": False, "type": 0, "webhook_id": "1" if webhook else None})
        self.typed.append({"id": message, "channel": channel, "content": text, "author": _user(user, f"user{user}", bot=bot)})
        return message

    def remove_the_bot(self, guild: str = GUILD) -> None:
        """Someone kicking the bot. As Discord: its commands and webhooks go with it, and the cards they posted stay, with nothing to edit them."""
        self.commands.pop(guild, None)
        self.webhooks.clear()
        self.dispatch("GUILD_DELETE", {"id": guild})

    def add_the_bot(self, guild: str = GUILD) -> None:
        """No `unavailable` in it: that's how a Discord says it's a new one, rather than one back from an outage."""
        self.dispatch("GUILD_CREATE", {**self.guild, "id": guild, "roles": self.roles, "channels": self.channels, "members": [], "threads": [], "presences": [], "voice_states": [], "stage_instances": [],
                                       "guild_scheduled_events": [], "soundboard_sounds": [], "member_count": 3, "large": False, "joined_at": _stamp()})

    def member(self, user: str, *, roles: tuple[str, ...] = (), permissions: str = "0") -> dict[str, Any]:
        return {"user": _user(user, f"user{user}"), "roles": list(roles), "joined_at": _stamp(), "permissions": permissions,
                "deaf": False, "mute": False, "flags": 0, "nick": None, "avatar": None, "premium_since": None, "pending": False,
                "communication_disabled_until": None}

    def _interaction(self, kind: int, data: dict[str, Any], user: str, roles: tuple[str, ...], guild: str, channel: str,
                     **extra: Any) -> tuple[str, str]:
        interaction, token = self.next_id(), f"answer{self.next_id()}"
        self.interactions_at[token] = time.monotonic()
        self.dispatch("INTERACTION_CREATE", {
            "id": interaction, "application_id": APPLICATION, "type": kind, "token": token, "version": 1, "data": data,
            "guild_id": guild, "channel_id": channel, "channel": {**self._channel(channel, "c"), "guild_id": guild},
            "member": self.member(user, roles=roles), "app_permissions": ALL_BUT_ADMIN, "locale": "en-US", "guild_locale": "en-US",
            "entitlements": [], "authorizing_integration_owners": {"0": guild}, "context": 0, "attachment_size_limit": 1000000,
            **extra})
        return interaction, token

    def huragok(self, command: str, *, user: str = OWNER_USER, roles: tuple[str, ...] = (), guild: str = GUILD,
                channel: str = PUBLIC_CHANNEL, **options: str) -> tuple[str, str]:
        """Someone typing /huragok <command>: (the interaction's ID, its token)."""
        inner = [{"name": key, "type": 3, "value": value} for key, value in options.items()]
        return self._interaction(2, {"id": self.next_id(), "name": "huragok", "type": 1,
                                     "options": [{"name": command, "type": 1, "options": inner}]},
                                 user, roles, guild, channel)

    def slash(self, group: str, command: str, *, user: str = OWNER_USER, roles: tuple[str, ...] = (), channel: str = PUBLIC_CHANNEL, **options: Any) -> tuple[str, str]:
        """Someone typing /group command: (the interaction's ID, its token)."""
        inner = [{"name": key, "type": 4 if isinstance(value, int) else 3, "value": value} for key, value in options.items()]
        return self._interaction(2, {"id": self.next_id(), "name": group, "type": 1, "options": [{"name": command, "type": 1, "options": inner}]},
                                 user, roles, GUILD, channel)

    def command(self, name: str, /, *, user: str = OWNER_USER, roles: tuple[str, ...] = (), channel: str = PUBLIC_CHANNEL, **options: Any) -> tuple[str, str]:
        """Someone typing /name, a command of its own rather than one of a group."""
        inner = [{"name": key, "type": 4 if isinstance(value, int) else 3, "value": value} for key, value in options.items()]
        return self._interaction(2, {"id": self.next_id(), "name": name, "type": 1, "options": inner}, user, roles, GUILD, channel)

    def command_with_file(self, name: str, option: str, filename: str, content: str, *, user: str = OWNER_USER, roles: tuple[str, ...] = (), channel: str = PUBLIC_CHANNEL, **options: Any) -> tuple[str, str]:
        """Someone typing /name, and attaching a file to it."""
        self.attachments[filename] = content
        attachment = self.next_id()
        address = f"{self.base.removesuffix('/api/v10')}/cdn/{filename}"
        inner = [{"name": key, "type": 3, "value": value} for key, value in options.items()] + [{"name": option, "type": 11, "value": attachment}]
        resolved = {"attachments": {attachment: {"id": attachment, "filename": filename, "size": len(content), "url": address, "proxy_url": address, **({"content_type": "video/mp4"} if filename.startswith("clip") else {})}}}
        return self._interaction(2, {"id": self.next_id(), "name": name, "type": 1, "options": inner, "resolved": resolved}, user, roles, GUILD, channel)

    def typing_command(self, name: str, option: str, typed: str, *, user: str = OWNER_USER, roles: tuple[str, ...] = (), **others: Any) -> tuple[str, str]:
        inner = [{"name": key, "type": 3, "value": value} for key, value in others.items()] + [{"name": option, "type": 3, "value": typed, "focused": True}]
        return self._interaction(4, {"id": self.next_id(), "name": name, "type": 1, "options": inner}, user, roles, GUILD, PUBLIC_CHANNEL)

    def typing(self, group: str, command: str, option: str, typed: str, *, user: str = OWNER_USER, roles: tuple[str, ...] = (), **others: Any) -> tuple[str, str]:
        """Someone typing in a slash command's option, with suggestions wanted."""
        inner = [{"name": key, "type": 3, "value": value} for key, value in others.items()] + [{"name": option, "type": 3, "value": typed, "focused": True}]
        return self._interaction(4, {"id": self.next_id(), "name": group, "type": 1, "options": [{"name": command, "type": 1, "options": inner}]},
                                 user, roles, GUILD, PUBLIC_CHANNEL)

    def press(self, custom_id: str, post: dict[str, Any], *, user: str = OWNER_USER, roles: tuple[str, ...] = ()) -> tuple[str, str]:
        """Someone pressing a button on something posted. Discord sends the message along; without Message Content a webhook's comes without its text."""
        message = {**self._message_form(post["channel"], {}), "id": post["id"], "webhook_id": "1"}
        return self._interaction(3, {"custom_id": custom_id, "component_type": 2}, user, roles, GUILD, post["channel"], message=message)

    def choose(self, custom_id: str, post: dict[str, Any], values: list[str], *, user: str = OWNER_USER, roles: tuple[str, ...] = ()) -> tuple[str, str]:
        """Someone picking from a menu on something posted."""
        message = {**self._message_form(post["channel"], {}), "id": post["id"], "webhook_id": "1"}
        return self._interaction(3, {"custom_id": custom_id, "component_type": 3, "values": values}, user, roles, GUILD, post["channel"], message=message)

    def fill_in(self, token: str, post: dict[str, Any], answers: dict[str, str], *, user: str = OWNER_USER, roles: tuple[str, ...] = ()) -> tuple[str, str]:
        """Someone sending the form an interaction (by its token) opened. `answers`: label -> what they wrote or picked."""
        form = next(callback["data"] for callback in self.callbacks if callback["token"] == token and callback.get("type") == 9)
        filled = []
        for label in form["components"]:
            item = label["component"]
            value = answers.get(label["label"], "")
            filled.append({"type": 18, "id": label.get("id", 0), "component": {"type": item["type"], "custom_id": item["custom_id"], "id": item.get("id", 0),
                                                                               **({"value": value} if item["type"] == 4 else {"values": [value] if value else []})}})
        message = {**self._message_form(post["channel"], {}), "id": post["id"], "webhook_id": "1"}
        return self._interaction(5, {"custom_id": form["custom_id"], "components": filled}, user, roles, GUILD, post["channel"], message=message)

    def pick(self, opened: str, custom_id: str, kind: int, values: list[str], *, resolved: dict[str, Any] | None = None,
             user: str = OWNER_USER, roles: tuple[str, ...] = (), channel: str = PUBLIC_CHANNEL) -> tuple[str, str]:
        """Someone using a panel a slash command `opened` (its interaction's ID): a button or a menu."""
        data: dict[str, Any] = {"custom_id": custom_id, "component_type": kind, "values": values}
        if resolved:
            data["resolved"] = resolved
        message = {**self._message_form(channel, {"flags": 64}), "id": self._panels.setdefault(opened, self.next_id()),
                   "interaction_metadata": {"id": opened, "type": 2, "user": _user(user, f"user{user}"),
                                            "authorizing_integration_owners": {"0": GUILD}}}
        return self._interaction(3, data, user, roles, GUILD, channel, message=message)


def use_fake_discord(fake: FakeDiscord, monkeypatch: Any) -> None:
    """rest.py and discord.py, both ways, at the fake."""
    import yarl
    from discord import gateway as discord_gateway
    from discord import http as discord_http
    from discord.webhook import async_ as discord_webhook

    from huragok.discordbot import rest

    monkeypatch.setattr(rest, "API", fake.base)
    monkeypatch.setattr(discord_http.Route, "BASE", fake.base)
    monkeypatch.setattr(discord_webhook.Route, "BASE", fake.base)
    monkeypatch.setattr(discord_gateway.DiscordWebSocket, "DEFAULT_GATEWAY", yarl.URL(fake.ws_url))
