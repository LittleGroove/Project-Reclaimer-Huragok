"""A fake Discord, as the fake `dedicated` is a fake server: its web API and its gateway on a port of this machine, keeping
what it was sent. It runs on a thread of its own, so a test can call rest.py's blocking functions directly as well as
run the real bot (discord.py) against it. `use_fake_discord` points rest.py and discord.py at it."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from typing import Any

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
TOKEN = "fake.bot.token"
ALL_BUT_ADMIN = str((1 << 40) | (1 << 29) | (1 << 28) | (1 << 16) | (1 << 15) | (1 << 14) | (1 << 11) | (1 << 10))


def _json(data: Any, *, status: int = 200, headers: dict[str, str] | None = None) -> web.Response:
    """As Discord sends it: discord.py reads an answer as JSON only when the type is exactly this."""
    return web.Response(body=json.dumps(data).encode("utf-8"), status=status, headers={"Content-Type": "application/json", **(headers or {})})


def _stamp() -> str:
    return "2026-10-30T00:00:00+00:00"


def _user(user_id: str, name: str, *, bot: bool = False) -> dict[str, Any]:
    return {"id": user_id, "username": name, "discriminator": "0", "avatar": None, "global_name": name, "bot": bot,
            "public_flags": 0, "flags": 0}


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
        self.posted: list[dict[str, Any]] = []  # what was posted: "channel", "via" ("webhook" or "bot") and the form sent
        self.commands: dict[str, list[dict[str, Any]]] = {}  # Discord -> what's registered
        self.registrations = 0
        self.callbacks: list[dict[str, Any]] = []  # answers to interactions (the form, plus "token")
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
        self._runner = web.AppRunner(app, handle_signals=False)
        await self._runner.setup()
        port = free_port()
        site = web.TCPSite(self._runner, "127.0.0.1", port)
        await site.start()
        self.base = f"http://127.0.0.1:{port}/api/v10"
        self.ws_url = f"ws://127.0.0.1:{port}/gateway"

    def stop(self) -> None:
        async def down() -> None:
            for socket in self._sockets:
                await socket.close()
            if self._runner is not None:
                await self._runner.cleanup()

        asyncio.run_coroutine_threadsafe(down(), self._loop).result(10)
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(10)

    def next_id(self) -> str:
        return str(next(self._ids))

    # The web API ---------------------------------------------------------------------------------------------

    async def _api(self, request: web.Request) -> web.Response:
        path = "/" + request.match_info["tail"]
        self.requests.append((request.method, path))
        for entry in list(self.refuse_next):
            part, status, body, headers = entry
            if part in path:
                self.refuse_next.remove(entry)
                return _json(body, status=status, headers=headers)
        body = await request.json() if request.can_read_body and request.content_type == "application/json" else None
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
            case ("PUT", ["applications", _, "guilds", guild, "commands"]):
                self.registrations += 1
                registered = [{**command, "id": self.next_id(), "application_id": APPLICATION, "guild_id": guild, "version": "1"}
                              for command in body]
                self.commands[guild] = registered
                return _json(registered)
            case ("GET", ["channels", channel, "webhooks"]):
                return _json([hook for hook in self.webhooks.values() if hook["channel_id"] == channel])
            case ("POST", ["channels", channel, "webhooks"]):
                hook_id = self.next_id()
                hook = {"id": hook_id, "type": 1, "name": body["name"], "channel_id": channel, "guild_id": GUILD,
                        "token": f"hook{hook_id}", "url": f"{self.base}/webhooks/{hook_id}/hook{hook_id}"}
                self.webhooks[hook_id] = hook
                return _json(hook)
            case ("POST", ["channels", channel, "messages"]):
                return self._message(channel, "bot", body)
            case ("GET", ["webhooks", hook, token]) if self.webhooks.get(hook, {}).get("token") == token:
                return _json(self.webhooks[hook])
            case ("POST", ["webhooks", hook, token]) if self.webhooks.get(hook, {}).get("token") == token:
                return self._message(self.webhooks[hook]["channel_id"], "webhook", body)
            case ("POST", ["webhooks", application, token]) if application == APPLICATION:  # an interaction's follow-up
                self.followups.append({**(body or {}), "token": token})
                return _json(self._message_form(PUBLIC_CHANNEL, body or {}))
            case ("GET" | "PATCH", ["webhooks", _, token, "messages", "@original"]):
                return _json(self._message_form(PUBLIC_CHANNEL, body or {}))
            case ("POST", ["interactions", interaction, token, "callback"]):
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

    def _message(self, channel: str, via: str, body: dict[str, Any]) -> web.Response:
        self.posted.append({**body, "channel": channel, "via": via})
        return _json(self._message_form(channel, body),
                                 headers={"X-RateLimit-Remaining": "4", "X-RateLimit-Reset-After": "1"})

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

    def member(self, user: str, *, roles: tuple[str, ...] = (), permissions: str = "0") -> dict[str, Any]:
        return {"user": _user(user, f"user{user}"), "roles": list(roles), "joined_at": _stamp(), "permissions": permissions,
                "deaf": False, "mute": False, "flags": 0, "nick": None, "avatar": None, "premium_since": None, "pending": False,
                "communication_disabled_until": None}

    def _interaction(self, kind: int, data: dict[str, Any], user: str, roles: tuple[str, ...], guild: str, channel: str,
                     **extra: Any) -> tuple[str, str]:
        interaction, token = self.next_id(), f"answer{self.next_id()}"
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
