"""Every feature of the bot, what each one needs, and the state it's in.

A feature is **off** (its switch), **waiting** (switched on but missing something, and this says exactly what), **on**, or
while 0.6.0 is built stage by stage, **coming** (its stage isn't in this version, so it can't be switched on). `check()` is
what /huragok check and the window's check show: every feature that's on, everything it needs, with a tick or what to
fix. It's the conflicts table in plans/discord-bot-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import rest
from ..core.tomledit import add_to_list, remove_from_list, set_nested_key
from .config import RANK_KEYS, DiscordConfig, set_channel
from .facts import Facts, gather
from .rest import PERMISSION_NAMES, DiscordError

BUILT = 1  # the stage of plans/discord-bot-design.md this version has got to

OFF, WAITING, ON, COMING = "off", "waiting", "on", "coming"
MARKS = {OFF: "⬜", WAITING: "⏳", ON: "✅", COMING: "🔜"}

POSTS = ("view", "send", "embed")  # what posting cards in a channel needs


@dataclass(frozen=True)
class Feature:
    key: str  # its name, and usually its table: "feed.moderation"
    title: str
    stage: int  # the stage that builds it
    needs_bot: bool = True  # False: a webhook can do it
    section: tuple[str, ...] = ()  # its table, when that isn't `key`
    switch: str = "enabled"
    on_value: Any = True  # what the switch is set to for on; anything but `off_value` counts as on
    off_value: Any = False
    channel: bool = False  # it posts in the channel its `channel` setting names
    staff: bool = False  # what it posts is private: never where @everyone can read, unless public = true
    permissions: tuple[str, ...] = ()  # in the Discord as a whole
    intents: tuple[str, ...] = ()  # privileged: the switches on the developer portal's Bot page
    needs_link: bool = False

    @property
    def table(self) -> tuple[str, ...]:
        return self.section or tuple(self.key.split("."))

    def is_on(self, config: DiscordConfig) -> bool:
        return config.section(*self.table).get(self.switch, self.off_value) != self.off_value


def _feed(name: str, title: str, *, staff: bool = True) -> Feature:
    return Feature(f"feed.{name}", title, 2, needs_bot=False, channel=True, staff=staff)


def _game(name: str, title: str) -> Feature:
    return Feature(f"games.{name}", title, 6)


def _community(key: str, title: str, *, permissions: tuple[str, ...] = (), on_value: Any = True,
               off_value: Any = False) -> Feature:
    return Feature(f"community.{key}", title, 6, section=("community",), switch=key, permissions=permissions,
                   on_value=on_value, off_value=off_value)


FEATURES: tuple[Feature, ...] = (
    Feature("personas", "Posting as the Superintendent, Vergil and Huragok", 1, needs_bot=False,
            permissions=("manage_webhooks",)),
    # Stage 2: feeds and status
    _feed("moderation", "Moderation feed"), _feed("vergil", "Vergil's feed"), _feed("votes", "Vote kicks"),
    _feed("alerts", "Alerts"), _feed("servers", "Server events"), _feed("games", "Games started and ended", staff=False),
    _feed("joins", "Joins"), _feed("kills", "Kill feed", staff=False), _feed("watch", "Watch list"),
    _feed("calls", "Calls for an admin"), _feed("content", "Content changes"), _feed("audit", "Audit log"),
    Feature("status.board", "Status board", 2, needs_bot=False, channel=True),
    Feature("status.seeding", "Seeding pings", 2, needs_bot=False, channel=True),
    Feature("status.presence", "The bot's status line", 2),
    Feature("status.bio", "The bot's bio", 2),
    Feature("status.nickname", "The bot's nickname", 2),
    Feature("status.channel_names", "Channel names", 2, permissions=("manage_channels",)),
    Feature("status.topics", "Channel topics", 2, permissions=("manage_channels",)),
    Feature("status.voice_status", "Voice channel status", 2, permissions=("voice_status",)),
    Feature("notes", "Notes and flags on players", 2),
    # Stage 3: console and staff tools
    Feature("console", "Typed console", 3, switch="typed", intents=("message_content",)),
    Feature("commands", "Slash commands", 3),
    Feature("reports", "Reports", 3, channel=True, staff=True),
    Feature("appeals", "Appeals", 3, channel=True, staff=True),
    Feature("tickets", "Tickets", 3, channel=True, permissions=("create_threads", "manage_threads")),
    # Stage 4: links and roles
    Feature("link", "Linking accounts to players", 4),
    Feature("roles", "Roles kept in step with ranks", 4, switch="sync", permissions=("manage_roles",),
            intents=("members",), needs_link=True),
    Feature("moderation", "Moderating the Discord", 4, permissions=("moderate",)),
    # Stage 5: stats
    Feature("stats", "Stats", 5),
    Feature("achievements", "Medals", 5, channel=True),
    Feature("leaderboards", "Leaderboards", 5, channel=True),
    Feature("matches", "Match summaries", 5, channel=True),
    Feature("chat.to_discord", "Game chat into Discord", 5, needs_bot=False, section=("chat",), switch="to_discord"),
    Feature("chat.to_game", "Discord chat into the game", 5, section=("chat",), switch="to_game",
            intents=("message_content",)),
    # Stage 6: community and games
    _community("welcome", "Welcome messages"), _community("first_game_post", "A member's first game"),
    _community("server_list", "Server list"), _community("game_nights", "Game nights", permissions=("manage_events",)),
    _community("polls_to_game", "Polls into the game"), _community("announce", "/announce"),
    _community("milestones", "Community milestones"), _community("server_birthdays", "Server birthdays"),
    _community("balance", "Balanced teams", on_value="command", off_value="off"),
    _community("team_voice", "Team voice channels", permissions=("move_members",)),
    _community("levels", "Levels from playtime", permissions=("manage_roles",)),
    _community("weekly_recap", "Weekly recap"), _community("host_digest", "The host's digest"),
    _community("awards", "Awards", on_value="weekly", off_value="off"),
    _game("record", "/record: the service record"), _game("carnage_report", "The Carnage Report"),
    _game("audio_logs", "Sadie's Story"), _game("skulls", "Skulls"), _game("drop_pod", "/drop: the daily check-in"),
    _game("payphone", "The payphone"), _game("kiosk", "/kiosk trivia"), _game("data_hive", "The Data Hive"),
    _game("repair", "The Huragok's repair"), _game("huragok_name", "/huragokname"), _game("birthdays", "Grunt birthdays"),
    _game("rare_lines", "Rare city lines"), _game("forecast", "Traffic Forecast"),
    _game("ballot_forecast", "Ballot forecast"), _game("bounty", "Wanted: the bounty"), _game("king", "Hail to the King"),
    _game("seven_on_seven", "7 on the 7th"), _game("wave_meter", "The wave meter"),
    _game("be_like_marty", "Be Like Marty"), _game("linktacular", "Linktacular"), _game("last_train", "The last train"),
    _game("fireteams", "Fireteams"), _game("rookie", "The Rookie"), _game("traffic_signs", "Traffic signs"),
    _game("drop_call", "/dropcall"), _game("friends", "Friends"), _game("passport", "The VISR passport"),
    _game("forge_contest", "The Forge contest"), _game("good_citizens", "Good citizens"),
)
BY_KEY = {feature.key: feature for feature in FEATURES}

# What the bot asks for whatever is switched on: posting, and answering in threads later.
BASE_PERMISSIONS = ("view", "send", "embed", "attach", "history", "manage_webhooks")


def options_of(feature: Feature, config: DiscordConfig) -> dict[str, Any]:
    """Its settings; a feed's with [feeds] under them."""
    if feature.key.startswith("feed."):
        return config.feed(feature.key.removeprefix("feed."))
    return config.section(*feature.table)


def channel_of(feature: Feature, config: DiscordConfig, facts: Facts | None) -> dict[str, Any] | None:
    if not feature.channel or facts is None:
        return None
    return facts.find_channel(config.channel_ref(options_of(feature, config).get("channel")))


def _missing_permissions(names: tuple[str, ...], facts: Facts, channel: dict[str, Any] | None = None) -> list[str]:
    return [PERMISSION_NAMES[name] for name in names if not facts.has(name, channel)]


def why_waiting(feature: Feature, config: DiscordConfig, facts: Facts | None) -> str:
    """What a switched-on feature still needs; "" when nothing."""
    options = options_of(feature, config)
    if config.mode == "off":
        return "the bot is off (mode = off)"
    if config.mode == "webhook":
        if feature.needs_bot:
            return "it needs the bot: a webhook can only post"
        if feature.channel and not str(options.get("webhook") or "").strip():
            return "it needs its channel's webhook address (webhook = …)"
        return ""
    if facts is None:
        return "Discord can't be read just now"
    if facts.guild is None:
        return "the bot isn't in its Discord yet"
    if feature.needs_link and not config.section("link").get("enabled"):
        return "it needs linking: [link] enabled = true"
    for intent in feature.intents:
        if not facts.intent_on(intent):
            switch = {"message_content": "Message Content", "members": "Server Members"}[intent]
            return f"the {switch} switch is off in the developer portal (the Bot page)"
    if feature.permissions and (missing := _missing_permissions(feature.permissions, facts)):
        return f"the bot's role is missing {', '.join(missing)}"
    if feature.channel:
        reference = config.channel_ref(options.get("channel"))
        if not reference:
            return f"it needs a channel: [channels] {options.get('channel')} is empty"
        channel = facts.find_channel(reference)
        if channel is None:
            return f"there's no channel '{reference}' in {facts.guild.get('name')}"
        if missing := _missing_permissions(POSTS, facts, channel):
            return f"the bot can't post in #{channel.get('name')} (missing {', '.join(missing)} there)"
        if feature.staff and facts.everyone_reads(channel) and not options.get("public"):
            return (f"#{channel.get('name')} can be read by @everyone, and this feature posts private things: make "
                    "it private, or set public = true")
    return ""


def state_of(feature: Feature, config: DiscordConfig, facts: Facts | None) -> tuple[str, str]:
    """(state, why). A feature whose stage isn't built counts as coming, whatever its switch says."""
    if feature.stage > BUILT:
        return COMING, f"arrives in a later beta (stage {feature.stage})"
    if not feature.is_on(config):
        return OFF, ""
    why = why_waiting(feature, config, facts)
    return (WAITING, why) if why else (ON, "")


def switched(feature: Feature, config: DiscordConfig) -> Any:
    """The value of its switch that means the other state from now."""
    return feature.off_value if feature.is_on(config) else feature.on_value


def invite_permissions(config: DiscordConfig) -> int:
    """Exactly what the features switched on need, for the invite link."""
    names = set(BASE_PERMISSIONS)
    for feature in FEATURES:
        if feature.is_on(config):
            names.update(feature.permissions)
    bits = 0
    for name in names:
        bits |= rest.PERMISSIONS[name]
    return bits


def needed_intents(config: DiscordConfig) -> set[str]:
    """The privileged intents what's switched on (and built) wants."""
    return {intent for feature in FEATURES if feature.stage <= BUILT and feature.is_on(config)
            for intent in feature.intents}


def names_to_ids(config: DiscordConfig, facts: Facts) -> list[str]:
    """Channels and roles typed by name, replaced with their IDs the first time Discord can say which they are, the name
    kept as a note: a renamed channel or role then keeps working. Returns what changed. It blocks."""
    path = config.path
    if path is None or facts.guild is None:
        return []
    changed: list[str] = []
    for key, reference in config.section("channels").items():
        text = str(reference).strip()
        channel = facts.find_channel(text) if text and not text.isdecimal() else None
        if channel is not None:
            set_channel(path, key, str(channel.get("id")), str(channel.get("name")))
            changed.append(f"[channels] {key}")
    ranks = config.section("ranks")
    for key in RANK_KEYS.values():
        for entry in ranks.get(key, []):
            text = str(entry).strip()
            role = facts.find_role(text) if text and not text.isdecimal() else None
            if role is not None:
                remove_from_list(path, "ranks", key, str(entry))
                add_to_list(path, "ranks", key, str(role.get("id")), f"@{role.get('name')}")
                changed.append(f"[ranks] {key}")
    for feature in FEATURES:
        if not feature.channel:
            continue
        written = str(config.section(*feature.table).get("channel") or "").strip()
        if not written or written.isdecimal() or written in config.section("channels"):
            continue
        channel = facts.find_channel(written)
        if channel is not None:
            set_nested_key(path, feature.table, "channel", str(channel.get("id")))
            changed.append(f"[{feature.key}] channel")
    return changed


# The check -----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Finding:
    good: bool
    where: str
    text: str


def _channel_privacy(config: DiscordConfig, facts: Facts) -> list[Finding]:
    """One channel, one audience: two features that disagree about its privacy can't share it."""
    found: list[Finding] = []
    seen: dict[str, tuple[Feature, bool]] = {}
    for feature in FEATURES:
        if not feature.channel or not feature.is_on(config) or feature.stage > BUILT:
            continue
        channel = channel_of(feature, config, facts)
        if channel is None:
            continue
        private = feature.staff and not options_of(feature, config).get("public")
        first = seen.setdefault(str(channel.get("id")), (feature, private))
        if first[1] != private:
            found.append(Finding(False, feature.title, f"it and {first[0].title} share #{channel.get('name')}, but one "
                                                       "is private and the other public: give them a channel each"))
    return found


def _ranks(config: DiscordConfig, facts: Facts) -> list[Finding]:
    ranks = config.section("ranks")
    found: list[Finding] = []
    owners = [str(owner).strip() for owner in ranks.get("owners", [])]
    for owner in owners:
        if not owner.isdecimal():
            found.append(Finding(False, "Owners", f"'{owner}' isn't a Discord user ID: owners are people, never a role"))
    if not owners:
        found.append(Finding(False, "Owners", "nobody can set the bot up from Discord yet: get a code from Huragok's "
                                              "window (J) and type /huragok claim"))
    else:
        found.append(Finding(True, "Owners", f"{len(owners)} named"))
    for rank, key in RANK_KEYS.items():
        for ref in ranks.get(key, []):
            role = facts.find_role(ref)
            if role is None:
                found.append(Finding(False, f"Rank {rank}", f"there's no role '{ref}' in {(facts.guild or {}).get('name')}"))
            else:
                found.append(Finding(True, f"Rank {rank}", f"@{role.get('name')}"))
    return found


def _channels(config: DiscordConfig, facts: Facts) -> list[Finding]:
    found: list[Finding] = []
    for name, reference in config.section("channels").items():
        if not str(reference).strip():
            continue
        channel = facts.find_channel(reference)
        if channel is None:
            found.append(Finding(False, f"Channel {name}", f"there's no channel '{reference}'"))
        elif missing := _missing_permissions(POSTS, facts, channel):
            found.append(Finding(False, f"Channel {name}", f"the bot can't post in #{channel.get('name')} (missing "
                                                           f"{', '.join(missing)} there)"))
        else:
            found.append(Finding(True, f"Channel {name}", f"#{channel.get('name')}"))
    return found


def _webhooks(config: DiscordConfig) -> list[Finding]:
    """Every webhook address the file holds: each one asked (it needs no token)."""
    found: list[Finding] = []
    addresses = [("The launcher's webhook", str(config.feed("alerts").get("launcher_webhook") or "").strip())]
    for feature in FEATURES:
        if feature.channel and feature.is_on(config) and feature.stage <= BUILT:
            addresses.append((feature.title, str(options_of(feature, config).get("webhook") or "").strip()))
    for where, address in addresses:
        if not address:
            continue
        try:
            info = rest.webhook_info(address)
            found.append(Finding(True, where, f"its webhook '{info.get('name')}' works"))
        except DiscordError as error:
            found.append(Finding(False, where, f"its webhook doesn't work: {error}"))
    return found


def check(config: DiscordConfig) -> tuple[list[Finding], Facts | None]:
    """Everything, asked of Discord now (it blocks: on the event loop, use a thread). Bad news first."""
    found: list[Finding] = []
    facts: Facts | None = None
    if config.error:
        return [Finding(False, "discord.toml", config.error)], None
    found += [Finding(False, "discord.toml", line) for line in config.warnings]
    found.append(Finding(True, "Mode", config.mode))
    if config.mode == "bot":
        if not config.token:
            found.append(Finding(False, "Token", "mode is bot but there's no token: set it in Huragok's window (J)"))
        else:
            try:
                facts = gather(config.token, config.guild)
            except DiscordError as error:
                found.append(Finding(False, "Discord", str(error)))
    if facts is not None:
        found.append(Finding(True, "Token", f"signed in as {facts.user.get('username')}"))
        if facts.application.get("bot_public"):
            found.append(Finding(False, "The bot", "anyone can add it to their Discord: turn Public Bot off in the "
                                                   "developer portal (the Bot page)"))
        if not config.guild:
            found.append(Finding(False, "Its Discord", "none picked yet (the bot is in "
                                                       f"{len(facts.guilds)}): pick one in Huragok's window (J), or "
                                                       "type /huragok claim there"))
        elif facts.guild is None:
            found.append(Finding(False, "Its Discord", f"the bot isn't in Discord {config.guild}: invite it (the window's "
                                                       "J gives the link)"))
        else:
            found.append(Finding(True, "Its Discord", str(facts.guild.get("name"))))
            found += _ranks(config, facts) + _channels(config, facts) + _channel_privacy(config, facts)
    found += _webhooks(config)
    for feature in FEATURES:
        if not feature.is_on(config) or feature.stage > BUILT:
            continue
        state, why = state_of(feature, config, facts)
        found.append(Finding(state == ON, feature.title, why or "on"))
    return sorted(found, key=lambda finding: finding.good), facts


__all__ = ["BUILT", "BY_KEY", "COMING", "FEATURES", "Feature", "Finding", "MARKS", "OFF", "ON", "WAITING", "channel_of",
           "check", "invite_permissions", "names_to_ids", "needed_intents", "options_of", "state_of", "switched",
           "why_waiting"]
