"""Errors every layer of Huragok shares."""


class ServerError(Exception):
    """A server didn't answer as asked: slow, restarting, or not connected. Not the part asking failing (a part
    whose handler meets one is never switched off for it); rcon.RconError is one."""
