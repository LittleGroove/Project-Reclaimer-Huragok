"""Every feature of the bot, what each one needs, and the state it's in.

A feature is **off** (its switch), **waiting** (switched on but missing something, and this says exactly what) or **on**.
While a later version is in beta, one of its features can be **coming**: its stage isn't built yet, so it can't be switched on.
`check()` is what /huragok check and the window's check show: every feature that's on, everything it needs, with a tick or
what to fix. It's the conflicts table in plans/discord-bot-design.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from . import rest
from ..core.tomledit import add_to_list, remove_from_list, set_nested_key
from .config import DiscordConfig, FALLBACKS, pings_of, RANK_KEYS, set_channel
from .facts import Facts, gather
from .rest import PERMISSION_NAMES, DiscordError

BUILT = 5  # the stage of plans/discord-bot-design.md this version has got to (2.1 feeds, 2.2 status, 2.3 the watch list and calls, 2.4 notes and buttons; 4 linking, with its role sync still to come; 5 stats, being built)

OFF, WAITING, ON, COMING = "off", "waiting", "on", "coming"
MARKS = {OFF: "⬜", WAITING: "⏳", ON: "✅", COMING: "🔜"}

ANNOUNCEMENT = 5  # Discord's kind of channel
POSTS = ("view", "send", "embed")  # what posting cards in a channel needs
PRIVATE_CHANNELS = {"staff", "alerts", "console", "reports", "appeals", "logs", "huragok-logs"}  # [channels] names for what staff alone should read
READ_ONLY_CHANNELS = {"status", "info", "games", "stats"}  # for everyone to read, and nobody to type in
CHANNELS = {"status": "the server cards and nothing else", "info": "news: pings people asked for, seeding, restarts planned, updates", "games": "games ending, and playlists changing with the player count", "stats": "the leaderboards, kept up to date",
            "staff": "tickets: what Vergil did, and who to keep an eye on", "reports": "tickets: people asking staff for something (unset: staff)", "appeals": "tickets: people appealing what's against them (unset: reports)",
            "alerts": "tickets: what's wrong with the servers now (unset: staff)", "logs": "the record of what people did", "huragok-logs": "the record of what Vergil and the Superintendent did (unset: logs)",
            "console": "typing console lines"}


@dataclass(frozen=True)
class Feature:
    key: str  # its name, and usually its table: "feed.moderation"
    title: str
    stage: float  # the stage that builds it
    needs_bot: bool = True  # False: a webhook can do it
    section: tuple[str, ...] = ()  # its table, when that isn't `key`
    switch: str = "enabled"
    on_value: Any = True  # what the switch is set to for on; anything but `off_value` counts as on
    off_value: Any = False
    channel: bool = False  # it posts in the channel its `channel` setting names
    staff: bool = False  # what it posts is private: never where @everyone can read, unless public = true
    permissions: tuple[str, ...] = ()  # in the Discord as a whole
    intents: tuple[str, ...] = ()  # privileged: the switches on the developer portal's Bot page
    lines: str = ""  # how its lines in [messages] start

    @property
    def table(self) -> tuple[str, ...]:
        return self.section or tuple(self.key.split("."))

    def is_on(self, config: DiscordConfig) -> bool:
        return config.section(*self.table).get(self.switch, self.off_value) != self.off_value


def _feed(name: str, title: str, *, staff: bool = True, stage: float = 2.1) -> Feature:
    return Feature(f"feed.{name}", title, stage, needs_bot=False, channel=True, staff=staff, lines=f"{name}-")


FEATURES: tuple[Feature, ...] = (
    Feature("personas", "Posting as the Superintendent, Vergil and Huragok", 1, needs_bot=False,
            permissions=("manage_webhooks",)),
    # Stage 2: feeds and status
    _feed("moderation", "Moderation feed"), _feed("vergil", "Vergil's feed"), _feed("votes", "Votes"),
    _feed("alerts", "Alerts"), _feed("servers", "Server events", staff=False), _feed("games", "Games started and ended, playlists changed", staff=False),
    _feed("joins", "Joins"), _feed("watch", "Watch list", stage=2.3),
    _feed("calls", "Calls for an admin", stage=2.3), _feed("requests", "Probationary moderators' requests", stage=2.7), _feed("content", "Content changes"), _feed("audit", "Audit log"),
    Feature("status.board", "Status board", 2.2, needs_bot=False, channel=True),
    Feature("status.seeding", "Seeding pings", 2.3, needs_bot=False, channel=True, lines="seeding-"),
    Feature("notify", "Alerts members pick for themselves", 2.3, channel=True, lines="notify-"),
    Feature("status.presence", "The bot's status line", 2.2),
    Feature("status.bio", "The bot's bio", 2.2),
    Feature("status.nickname", "The bot's nickname", 2.2, permissions=("change_nickname",)),
    Feature("status.channel_names", "Channel names", 2.2, permissions=("manage_channels",)),
    Feature("status.topics", "Channel topics", 2.2, permissions=("manage_channels",)),
    Feature("status.voice_status", "Voice channel status", 2.2, permissions=("voice_status", "manage_channels")),
    Feature("notes", "Notes and flags on players", 2.4),
    # Stage 3: console and staff tools
    Feature("console", "Typed console", 2.9, switch="typed", intents=("message_content",), channel=True, staff=True),
    Feature("commands", "Every in-game command as a slash command", 2.6),
    Feature("reports", "Reports", 2.95, channel=True, staff=True, lines="reports-"),
    Feature("appeals", "Appeals", 2.96, channel=True, staff=True, lines="appeals-"),
    Feature("contact", "Contact staff", 3, channel=True, permissions=("create_private_threads", "send_in_threads", "manage_threads")),
    # Stage 4: links (0.6.1; role sync to come)
    Feature("link", "Linking players to their accounts here", 4),
    Feature("roles", "Rank roles from Huragok", 4, switch="sync", permissions=("manage_roles",), intents=("members",)),
    # Stage 5: stats (0.6.1)
    Feature("stats", "Stats and leaderboards", 5),
    Feature("leaderboards", "Leaderboards kept in a channel", 5, channel=True),
    Feature("sides", "Players on the team they chose", 5),
    Feature("side_roles", "Side roles", 5, section=("sides",), switch="roles", permissions=("manage_roles",), intents=("members",)),
    Feature("top_roles", "Top roles for the leaderboards", 5, section=("roles",), switch="top_roles", permissions=("manage_roles",), intents=("members",)),
)
BY_KEY = {feature.key: feature for feature in FEATURES}
FEEDS_ELSEWHERE = {"seeding": "status.seeding", "notify": "notify"}  # a card's feed, whose feature isn't feed.<its name>


def feature_of_feed(feed: str) -> Feature | None:
    """The feature a card of this feed belongs to."""
    return BY_KEY.get(FEEDS_ELSEWHERE.get(feed, f"feed.{feed}"))


# What each does, before it's switched on: titles alone don't say.
WHAT = {"personas": "Posts come from three names of their own, rather than the bot's", "feed.moderation": "A card for each mute, kick and ban staff give, in logs. Its buttons still work",
        "feed.vergil": "A ticket for each strike from the word list, to acknowledge or undo, and for each word or name of a list that only reports, to decide", "feed.votes": "A line for each vote, in logs", "feed.alerts": "A ticket when a server crashes, hangs or can't start, which closes itself when it's back",
        "feed.servers": "A line when a server starts, stops or restarts; news of restarts and updates in info", "feed.games": "A post when a game ends with at least min_players on (2 unless you change it). Starts too, once games-started is switched on in Messages. And a post when playlists by player count (G in the window, then P) put another playlist in rotation", "feed.joins": "A line for each join and leave, in logs",
        "feed.watch": "A ticket when someone flagged, or just kicked, or banned elsewhere joins", "feed.calls": "A ticket when a player types !calladmin", "feed.requests": "A ticket for a probationary moderator's request, to confirm or deny",
        "feed.content": "A line when a Workshop item changes", "feed.audit": "A line for what staff change or run from Discord", "status.board": "A live card per server, for everyone: who's on, what's playing",
        "status.seeding": "A post in info when a server is nearly empty and could use players", "notify": "/notify: anyone can ask to be pinged when a server reaches so many players, or a game type starts", "status.presence": "What the bot's status line says", "status.bio": "What the bot's bio says", "status.nickname": "The bot's nickname shows the player count",
        "status.channel_names": "A channel's name shows a count", "status.topics": "A channel's topic shows a count", "status.voice_status": "A voice channel's status shows a count", "notes": "/note and /flag: what staff write down about a player, shown on their cards",
        "console": "Lines typed in a private channel run on a server", "commands": "/kick, /whois and every other in-game command in Discord, with /server, /status, /ticket and /case", "reports": "/report and right-click a message: a ticket for staff",
        "appeals": "/appeal: a ticket staff can lift, shorten or deny, and the person's told", "contact": "A button anyone can press to open a private thread with staff",
        "link": "!link in game and /link here: a player's stats show under their name in this Discord",
        "stats": "/stats: a player's service record, and /leaderboard: the best at anything the servers count",
        "leaderboards": "Boards kept in a channel, brought up to date as games are reported, with Red against Blue",
        "sides": "!side or /side red or blue, for members and up: put on that team in the lobby or during a round, never emptying a team, the teams kept within most_apart",
        "roles": "A linked member wears the role of their rank in Huragok, and only Huragok gives those roles: one given by hand is taken off",
        "top_roles": "A role for the top of each leaderboard you pick, worn by the linked players there, moved as the board changes",
        "side_roles": "A role for Red and one for Blue, worn by linked members who chose that side: open a category to each"}
# What a first install is best off starting with: what staff need, and nothing that posts in public.
RECOMMENDED = ("feed.moderation", "feed.vergil", "feed.alerts", "feed.calls", "feed.requests", "feed.audit", "commands", "notes")
# Those whose message matters enough to set in one place: (what it's about, its lines in [messages]).
TOLD = (("A server in trouble", ("alerts-crashed", "alerts-faulted", "alerts-hung", "alerts-start-failed")), ("A player calling for an admin", ("calls-called",)),
        ("A report", ("reports-made", "reports-message")), ("Many reports of one player", ("reports-many",)), ("Someone opening a thread with staff", ("contact-opened",)), ("An appeal", ("appeals-made",)), ("A probationary moderator's request", ("requests-asked",)),
        ("A strike from Vergil", ("vergil-strike",)), ("A word or a name for staff to decide", ("vergil-heard", "vergil-name-asked")), ("A server needing players", ("seeding-wanted",)))


@dataclass(frozen=True)
class Step:
    done: bool
    label: str  # on its button
    text: str  # what it means, on Home
    screen: str


def missing_permissions(config: DiscordConfig, facts: Facts | None) -> list[str]:
    """What the bot's role lacks, for what's switched on: what the invite link would give it."""
    if facts is None or facts.guild is None:
        return []
    wanted = invite_permissions(config)
    places = [channel for channel in (facts.find_channel(config.channel_ref(name)) for name in ("console",)) if channel is not None]
    if BY_KEY["contact"].is_on(config) and (button := facts.find_channel(config.channel_ref(config.section("contact").get("channel")))):
        places.append(button)
    return [PERMISSION_NAMES[name] for name, bit in rest.PERMISSIONS.items() if wanted & bit and name in PERMISSION_NAMES and not facts.has(name) and not any(facts.has(name, place) for place in places)]  # given on the one channel it's needed in is better than on the role


def steps(config: DiscordConfig, facts: Facts | None) -> list[Step]:
    """What's left to do, worked out fresh: each done or not, the next one the one to press."""
    on = [feature for feature in FEATURES if feature.stage <= BUILT and feature.is_on(config) and feature.key != "personas"]
    channels = config.section("channels")
    ranks = config.section("ranks")
    pinged = any(pings_of(config.section("messages").get(key) or {}) for _, keys in TOLD for key in keys)
    waiting = [feature.title for feature in on if state_of(feature, config, facts)[0] == WAITING]
    missing = missing_permissions(config, facts)
    return [Step(bool(on), "What it does", f"**What it does:** {len(on)} switched on" if on else "**What it does:** nothing's switched on. Recommended turns on what staff need", "features"),
            Step(bool(str(channels.get("staff") or "").strip() and str(channels.get("logs") or "").strip()), "Channels", "**Channels:** staff and logs are all a small Discord needs. Tickets go in one and the record in the other", "channels"),
            Step(not missing, "Permissions", "**Permissions:** the bot's role has what it needs" if not missing else f"**Permissions:** the bot's role lacks {', '.join(missing[:6])}. The invite link below adds them", "features"),
            Step(any(ranks.get(key) for key in RANK_KEYS.values()), "Ranks", "**Ranks:** which of your roles are moderators and admins. Until then only faculty can press anything", "ranks"),
            Step(pinged, "Who gets told", "**Who gets told:** a role to ping when a server goes down or someone calls for an admin. Until then nobody's phone buzzes", "told"),
            Step(not waiting, "Waiting", "**Everything on is working**" if not waiting else f"**Waiting:** {', '.join(waiting[:6])}. Features says why", "features")]



# What the bot asks for whatever is switched on: posting, and answering in threads later.
BASE_PERMISSIONS = ("view", "send", "embed", "attach", "history", "manage_webhooks")


def options_of(feature: Feature, config: DiscordConfig) -> dict[str, Any]:
    """Its settings; a feed's with [feeds] under them."""
    if feature.key.startswith("feed."):
        return config.feed(feature.key.removeprefix("feed."))
    return config.section(*feature.table)


def slash_commands(config: DiscordConfig) -> set[str]:
    """The commands beyond /huragok that are wanted now. They come and go with their feature, registered only when that changes."""
    wanted = {"note", "flag"} if BY_KEY["notes"].is_on(config) else set()
    if BY_KEY["notify"].is_on(config):
        wanted.add("notify")
    if BY_KEY["commands"].is_on(config) and config.section("commands").get("game_commands", True):
        wanted.add("game")
    if BY_KEY["commands"].is_on(config):
        wanted |= {"ticket", "server", "status", "case", "help", "tickets"}
    if BY_KEY["appeals"].is_on(config):
        wanted.add("appeal")
    if BY_KEY["link"].is_on(config):
        wanted |= {"link", "unlink"}
    if BY_KEY["stats"].is_on(config):
        wanted |= {"stats", "leaderboard"}
    if BY_KEY["sides"].is_on(config) and config.mode == "bot":
        wanted |= {"side", "togglesides"}
    if BY_KEY["reports"].is_on(config) and "discord" in (config.section("reports").get("from") or []):
        wanted.add("report")
        if config.section("reports").get("right_click", True):
            wanted.add("report-message")
    return wanted


def _every_line(feature: Feature, config: DiscordConfig) -> dict[str, dict[str, Any]]:
    """A feed's lines in [messages], posting or not."""
    if not feature.lines:
        return {}
    return {key: line for key, line in config.section("messages").items() if key.startswith(feature.lines) and isinstance(line, dict)}


def message_lines(feature: Feature, config: DiscordConfig) -> dict[str, dict[str, Any]]:
    """A feed's lines in [messages] that post."""
    return {key: line for key, line in _every_line(feature, config).items() if line.get("post", True)}


def channels_of(feature: Feature, config: DiscordConfig) -> list[tuple[str, str]]:
    """(what it's called in the file, what that means) for each channel this feature posts in."""
    lines = message_lines(feature, config)
    named = [str(line.get("channel") or "") for line in lines.values()] if lines else [str(options_of(feature, config).get("channel") or "")]
    if feature.key == "console" and options_of(feature, config).get("layout") == "per-server" and (own := [str(reference) for reference in dict(options_of(feature, config).get("channels") or {}).values()]):
        named = own + [name for name in named if config.channel_ref(name)]  # with a channel for each, the picker's is only there if it's set
    return [(name, config.channel_ref(name)) for name in dict.fromkeys(named)]


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
        if feature.channel and not str(options.get("webhook") or "").strip() and not any(
                str(line.get("webhook") or "").strip() for line in message_lines(feature, config).values()):
            return "it needs its channel's webhook address (webhook = …)"
        return ""
    if facts is None:
        return "Discord can't be read just now"
    if facts.guild is None:
        return "the bot isn't in its Discord yet"
    for intent in feature.intents:
        if not facts.intent_on(intent):
            switch = {"message_content": "Message Content Intent", "members": "Server Members Intent"}[intent]
            return f"{switch} is off in the developer portal (the Bot page, under Privileged Gateway Intents)"
    where = facts.find_channel(config.channel_ref(config.section("contact").get("channel"))) if feature.key == "contact" else None  # its threads are in one channel: it needn't be able to see every private thread in the Discord
    if feature.permissions and (missing := _missing_permissions(feature.permissions, facts, where)):
        return f"the bot's role is missing {', '.join(missing)}" + (f" in #{where.get('name')}" if where else "")
    if feature.channel and _every_line(feature, config) and not message_lines(feature, config):
        return "all its messages are switched off: switch one on in Messages"
    if feature.channel:
        for name, reference in channels_of(feature, config):
            if not reference:
                fallback = FALLBACKS.get(name)
                return f"it needs a channel: pick {name}" + (f", or {fallback}, which it falls back on," if fallback else "") + " in Channels"
            channel = facts.find_channel(reference)
            if channel is None:
                return f"there's no channel '{reference}' in {facts.guild.get('name')}"
            if missing := _missing_permissions(POSTS, facts, channel):
                return f"the bot can't post in #{channel.get('name')} (missing {', '.join(missing)} there)"
            if feature.staff and facts.everyone_reads(channel) and not (config.section("console").get("public") if feature.key == "console" else options.get("public") or config.section("feeds").get("public")):
                return (f"#{channel.get('name')} can be read by @everyone, and this feature posts private things: make it private, or allow it" + (" on the Console panel" if feature.key == "console" else " on the Channels panel"))
    if feature.key == "contact":
        return _contact_waiting(config, facts)
    if feature.key in ROLE_FEATURES:
        return _roles_waiting(feature, config, facts)
    return ""


ROLE_FEATURES = ("roles", "top_roles", "side_roles")  # the roles Huragok gives, and takes off whoever shouldn't wear them


def given_roles(key: str, config: DiscordConfig) -> list[tuple[str, str]]:
    """(the role as written, the rank it stands for, or "" for a top or side role) for each role a feature gives."""
    if key == "roles":
        ranks = config.section("ranks")
        found = [(str(ref), rank) for rank, name in RANK_KEYS.items() for ref in ranks.get(name) or []]
        found += [(str(block.get("role") or ""), str(block.get("rank", "")).strip().replace("_", "-"))
                  for block in ranks.get("scoped") or [] if isinstance(block, dict)]
        found.append((str(config.section("roles").get("faculty") or ""), "faculty"))
    elif key == "top_roles":
        found = [(str(ref), "") for ref in (config.section("roles").get("top") or {}).values()]
    else:
        found = [(str(config.section("sides").get(f"{side}_role") or ""), "") for side in ("red", "blue")]
    return [(ref.strip(), rank) for ref, rank in found if ref.strip()]


def _roles_waiting(feature: Feature, config: DiscordConfig, facts: Facts) -> str:
    """A role Huragok gives is taken off everyone else, so each has to be one it can give, and one with no other job
    (audit 0.6.1, 3.1, 3.5, 3.8): a top role that's also a rank role made the top of a board staff here; one role on
    two ranks gave moderators an admin's; a role above the bot's own was refused at every look."""
    if not config.section("link").get("enabled"):
        return "linking is off ([link] enabled): only linked members wear the roles Huragok gives, so it would take every one off"
    top = facts.top_position()
    ranks_of: dict[str, set[str]] = {}
    for ref, rank in given_roles(feature.key, config):
        role = facts.find_role(ref)
        if role is None:  # on, giving nothing, and nothing said (second audit, 9.11)
            return f"there's no role {ref} in {facts.guild.get('name') if facts.guild else 'the Discord'}"
        name, role_id = role.get("name"), str(role.get("id"))
        if facts.guild is not None and role_id == str(facts.guild.get("id")):
            return "@everyone can't be one of the roles Huragok gives: pick a role of its own"
        if role.get("managed"):
            return f"@{name} belongs to an integration (a bot's role, or the boosters'): nobody can give it"
        if int(role.get("position") or 0) >= top:
            return f"@{name} isn't below the bot's own role: move the bot's role above it in Server Settings > Roles"
        ranks_of.setdefault(role_id, set()).add(rank)
    if feature.key != "roles":
        rank_roles = {str(role["id"]) for ref, _ in given_roles("roles", config) if (role := facts.find_role(ref))}
        if clash := next((facts.find_role(role_id) for role_id in ranks_of if role_id in rank_roles), None):
            what = "the top of a board" if feature.key == "top_roles" else "everyone on a side"
            return f"@{clash.get('name')} is a rank role ([ranks]): {what} would be staff here. Pick a role of its own"
    elif clash := next((facts.find_role(role_id) for role_id, ranks in ranks_of.items() if len(ranks) > 1), None):
        return f"@{clash.get('name')} stands for more than one rank in [ranks]: whoever wears it would count as the highest. Give each rank its own role"
    return ""


def _contact_waiting(config: DiscordConfig, facts: Facts) -> str:
    """A button whose thread nobody's told about is worse than none: its card has to have somewhere to go, and threads have to be possible where the button is."""
    button = facts.find_channel(config.channel_ref(config.section("contact").get("channel")))
    if button is not None and button.get("type") == ANNOUNCEMENT:
        return f"#{button.get('name')} is an announcement channel, where private threads can't be made: pick another for the button"
    line = config.section("messages").get("contact-opened") or {}
    if not line.get("post", True):
        return "contact-opened is switched off in Messages, so nobody would hear of a new thread"
    channel = facts.find_channel(config.channel_ref(line.get("channel") or "reports"))
    if channel is None:
        return "there's nowhere to tell staff about a new thread: pick reports or staff in Channels"
    if facts.everyone_reads(channel) and not config.section("feeds").get("public"):
        return f"#{channel.get('name')}, where staff are told about a new thread, can be read by @everyone: make it private, or allow it on the Channels panel"
    return ""


def state_of(feature: Feature, config: DiscordConfig, facts: Facts | None) -> tuple[str, str]:
    """(state, why). A feature whose stage isn't built counts as coming, whatever its switch says."""
    if feature.stage > BUILT:
        return COMING, "arrives in a later version"
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
        if feature.stage <= BUILT and feature.is_on(config):
            names.update(feature.permissions)
    if (config.section("tickets").get("threads", True) or config.section("tickets").get("when_closed") == "thread") and any(BY_KEY[key].is_on(config) for key in ("feed.moderation", "feed.vergil", "feed.watch", "feed.calls", "feed.alerts", "feed.requests", "reports", "appeals", "commands")):
        names.update(("create_threads", "send_in_threads"))
    if "thread" in {config.section("tickets").get("when_closed"), *(line.get("closes_to") for line in config.section("messages").values() if isinstance(line, dict))}:
        names.add("manage_threads")  # closing locks and hides a card's thread
    if BY_KEY["contact"].is_on(config):
        names.add("pin_messages")  # its button stays at hand
    if BY_KEY["console"].is_on(config):
        console = config.section("console")
        names.add("pin_messages")  # the picker's menu stays at hand
        if console.get("ignore_others") == "delete" or console.get("clear_after_minutes"):
            names.add("manage_messages")  # also so a password typed there can go at once
        if console.get("answers") == "thread":
            names.update(("create_threads", "send_in_threads"))
    bits = 0
    for name in names:
        bits |= rest.PERMISSIONS[name]
    return bits


def needed_intents(config: DiscordConfig) -> set[str]:
    """The privileged intents what's switched on (and built) wants."""
    wanted = {intent for feature in FEATURES if feature.stage <= BUILT and feature.is_on(config) for intent in feature.intents}
    if BY_KEY["contact"].is_on(config) and config.section("contact").get("transcript", True):
        wanted.add("message_content")  # to read back what was said. Without it the rest of it works, with no transcript
    return wanted


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
    # The other roles Huragok gives, one by one: written by name, another role of that name was never the one (audit 0.6.1, 3.14).
    from .config import set_switch

    for table, key in (("roles", "faculty"), ("sides", "red_role"), ("sides", "blue_role")):
        text = str(config.section(table).get(key) or "").strip()
        role = facts.find_role(text) if text and not text.isdecimal() else None
        if role is not None:
            set_switch(path, (table,), key, str(role.get("id")))  # type: ignore[arg-type]
            changed.append(f"[{table}] {key}")
    text = str(config.section("leaderboards").get("season_channel") or "").strip()
    channel = facts.find_channel(text) if text and not text.isdecimal() else None
    if channel is not None:  # by name: its ID, as [channels] are (9.11)
        set_switch(path, ("leaderboards",), "season_channel", str(channel.get("id")))  # type: ignore[arg-type]
        changed.append("[leaderboards] season_channel")
    top = config.section("roles").get("top") or {}
    if isinstance(top, dict):
        named = {entry: str(found["id"]) if (text := str(ref).strip()) and not text.isdecimal() and (found := facts.find_role(text)) else ref
                 for entry, ref in top.items()}
        if named != top:
            set_switch(path, ("roles",), "top", named)  # type: ignore[arg-type]
            changed.append("[roles] top")
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


def _ranks(config: DiscordConfig, facts: Facts) -> list[Finding]:
    ranks = config.section("ranks")
    found: list[Finding] = []
    faculty = [str(faculty).strip() for faculty in ranks.get("faculty", [])]
    for faculty in faculty:
        if not faculty.isdecimal():
            found.append(Finding(False, "Faculty", f"'{faculty}' isn't a Discord user ID: faculty are people, never a role"))
    if not faculty:
        found.append(Finding(False, "Faculty", "nobody can set the bot up from Discord yet: get a code from Huragok's "
                                              "window (J) and type /huragok claim"))
    else:
        found.append(Finding(True, "Faculty", f"{len(faculty)} named"))
    for rank, key in RANK_KEYS.items():
        for ref in ranks.get(key, []):
            role = facts.find_role(ref)
            if role is None:
                found.append(Finding(False, f"Rank {rank}", f"there's no role '{ref}' in {(facts.guild or {}).get('name')}"))
            else:
                found.append(Finding(True, f"Rank {rank}", f"@{role.get('name')}"))
                if rank != "member":  # members aren't staff: they're not meant to see any of this
                    found += _rank_can_see(rank, role, config, facts)
    if not any(ranks.get(key) for key in RANK_KEYS.values()):
        found.append(Finding(False, "Ranks", "no roles are mapped, so only faculty can press anything on a card: pick them on the Ranks panel"))
    if ranks.get(RANK_KEYS["probationary-moderator"]) and not BY_KEY["feed.requests"].is_on(config):
        found.append(Finding(False, "Rank probationary-moderator", "their commands become requests, and with Probationary moderators' requests off, Discord hears nothing of them: switch it on in Features"))
    return found


def _rank_can_see(rank: str, role: dict[str, Any], config: DiscordConfig, facts: Facts) -> list[Finding]:
    """A role given a rank that can't see its tickets, or whose slash commands stay hidden from it. Discord shows faculty and an administrator everything, so the host won't notice."""
    found: list[Finding] = []
    needed = str(config.section("commands").get("staff_permission") or "")
    if BY_KEY["commands"].is_on(config) and needed and needed in rest.DISCORD_FLAGS and not int(role.get("permissions") or 0) & (rest.DISCORD_FLAGS[needed] | rest.PERMISSIONS["administrator"]):
        found.append(Finding(False, f"Rank {rank}", f"@{role.get('name')} won't see /mute and the other staff commands, as it lacks {needed.replace('_', ' ')}: give it that, or pick who sees them on the Commands panel"))
    for name in ("staff", "reports", "appeals", "alerts"):
        channel = facts.find_channel(config.channel_ref(name))
        if channel is not None and not facts.role_can_see(role, channel):
            found.append(Finding(False, f"Rank {rank}", f"@{role.get('name')} can't see #{channel.get('name')}, so its tickets don't reach them: allow it in that channel's permissions"))
            break
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
        elif name in PRIVATE_CHANNELS and facts.everyone_reads(channel) and not config.section("feeds").get("public") and not (name == "console" and config.section("console").get("public")):
            found.append(Finding(False, f"Channel {name}", f"#{channel.get('name')} can be read by @everyone, and it's for "
                                                           "private things: make it private, or set [feeds] public = true"))
        elif name == "console" and facts.everyone_sends(channel) and config.section("console").get("typed") and config.section("console").get("ignore_others") != "delete":
            found.append(Finding(True, f"Channel {name}", f"#{channel.get('name')}: anyone can type there, and lines from people without a rank are ignored"))
        elif name in READ_ONLY_CHANNELS and facts.everyone_sends(channel):
            found.append(Finding(False, f"Channel {name}", f"anyone can type in #{channel.get('name')}, which is only for the bot to post in: take Send Messages away from @everyone there, and allow it for the bot's role"))
        else:
            found.append(Finding(True, f"Channel {name}", f"#{channel.get('name')}"))
    return found


def _contact(config: DiscordConfig, facts: Facts) -> list[Finding]:
    """Its threads are private, in a channel nobody may be able to type in, which is fine until they can't write in the thread either."""
    if not BY_KEY["contact"].is_on(config):
        return []
    channel = facts.find_channel(config.channel_ref(config.section("contact").get("channel")))
    if channel is None or facts.everyone_can("send_in_threads", channel):
        return []
    return [Finding(False, "Contact staff", f"people can't write back in their threads: allow Send Messages in Threads for @everyone in #{channel.get('name')}")]


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


def _others_who_can_read(config: DiscordConfig, facts: Facts) -> list[Finding]:
    """Private channels other roles can read: the channel isn't open to @everyone, and it can still be open to everyone, if everyone has a role like Member."""
    ranks = config.section("ranks")
    staff = {str(entry).strip().lstrip("@").casefold() for key in ("admin", "moderator", "probationary_moderator") for entry in ranks.get(key) or []}
    found: list[Finding] = []
    for name in sorted(PRIVATE_CHANNELS):
        channel = facts.find_channel(config.channel_ref(name))
        if channel is None or facts.everyone_reads(channel):
            continue
        readers = [str(role.get("name")) for role in facts.roles if str(role.get("id")) != str((facts.guild or {}).get("id")) and not role.get("managed") and str(role.get("id")) not in staff and str(role.get("name")).casefold() not in staff and facts.role_can_see(role, channel)]
        if readers:
            found.append(Finding(False, f"Channel {name}", f"#{channel.get('name')} is for staff, and {', '.join('@' + reader for reader in readers[:5])} can read it too: if those aren't staff, take View Channel away from them there"))
    return found


def _more_than_it_needs(facts: Facts) -> list[Finding]:
    """What whoever got hold of the token could do with it."""
    allowed = facts.base_permissions()
    if allowed & rest.PERMISSIONS["administrator"]:
        return [Finding(False, "The bot", "its role has Administrator, which it doesn't need: if its token ever leaks, that's what whoever has it can do. The invite link asks for only what's used")]
    extra = [label for label, bit in (("Ban Members", 1 << 2), ("Kick Members", 1 << 1)) if allowed & bit]
    return [Finding(False, "The bot", f"its role has {' and '.join(extra)}, which it doesn't use: if its token ever leaks, that's what whoever has it can do")] if extra else []


def _pings(config: DiscordConfig, facts: Facts) -> list[Finding]:
    """A ping for a role that's been deleted reaches nobody, and looks like it's working."""
    found: list[Finding] = []
    for key, line in config.section("messages").items():
        if isinstance(line, dict) and line.get("post", True) and any(facts.find_role(ping) is None for ping in pings_of(line)):
            found.append(Finding(False, f"Message {key}", f"it pings a role that isn't in {(facts.guild or {}).get('name')} any more: pick another in Who gets told"))
    return found


def _appeals(config: DiscordConfig) -> list[Finding]:
    """Nothing else tells someone who's banned that they can ask, or how."""
    if BY_KEY["appeals"].is_on(config) and not str(config.section("appeals").get("in_reason") or "").strip():
        return [Finding(False, "Appeals", "nothing tells someone who's banned that they can appeal: [appeals] in_reason is added to a ban's message, e.g. \"Appeal on discord.gg/yours: /appeal {code}\"")]
    return []


def _permissions_wanted(config: DiscordConfig, facts: Facts) -> list[Finding]:
    missing = missing_permissions(config, facts)
    return [Finding(False, "Permissions", f"the bot's role lacks {', '.join(missing)}, for what's on: tick them on its role, or open the invite link (it doesn't need removing first)")] if missing else []


def _clearing(config: DiscordConfig, facts: Facts) -> list[Finding]:
    from .consoleclear import problems

    return [Finding(False, "Typed console", problem) for problem in problems(config, facts)] if BY_KEY["console"].is_on(config) else []


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
            found.append(Finding(False, "The bot", "anyone can add it to their Discord: in the developer portal, set "
                                                   "Install Link to None (Installation), then turn Public Bot off (Bot)"))
        if not config.guild:
            found.append(Finding(False, "Its Discord", "none picked yet (the bot is in "
                                                       f"{len(facts.guilds)}): pick one in Huragok's window (J), or "
                                                       "type /huragok claim there"))
        elif facts.guild is None:
            found.append(Finding(False, "Its Discord", f"the bot isn't in Discord {config.guild}: invite it (the window's "
                                                       "J gives the link)"))
        else:
            found.append(Finding(True, "Its Discord", str(facts.guild.get("name"))))
            found += _ranks(config, facts) + _channels(config, facts) + _others_who_can_read(config, facts) + _contact(config, facts) + _clearing(config, facts) + _permissions_wanted(config, facts) + _more_than_it_needs(facts) + _pings(config, facts) + _appeals(config)
    found += _webhooks(config)
    for feature in FEATURES:
        if not feature.is_on(config) or feature.stage > BUILT:
            continue
        state, why = state_of(feature, config, facts)
        found.append(Finding(state == ON, feature.title, why or "on"))
    return sorted(found, key=lambda finding: finding.good), facts


__all__ = ["BUILT", "BY_KEY", "COMING", "FEATURES", "Feature", "Finding", "MARKS", "OFF", "ON", "WAITING", "channels_of",
           "check", "invite_permissions", "names_to_ids", "needed_intents", "options_of", "state_of", "switched",
           "why_waiting"]
