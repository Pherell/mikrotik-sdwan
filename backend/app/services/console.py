"""Type a RouterOS command, see what it says.

This is a **command console, not a shell**, and the difference is deliberate.

A PTY proxied to SSH would turn the controller into a jump host with a shell on
every device it manages: a controller compromise would go from "can push
configuration through the reconciler, with a diff and a rollback and an audit
row" to "has root on the whole estate". That is a real product, worth building
on purpose, and it is not this.

There is a second reason, and it is the one that would bite first in practice.
Configuration changed by hand here is drift the reconciler does not know about,
and the next apply reverts it -- silently, because reverting drift is exactly
its job. A console that lets you change things behind the reconciler's back
does not give you a faster way to work; it gives you changes that disappear.

So: reads and probes. Everything a `print` can answer, plus ping and
traceroute. Changes go through plan and apply, where they leave a diff, a
backup and a rollback behind them.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any

from app.drivers.base import DeviceDriver, DriverError

# Menus this console may read. Deliberately its own list rather than a reuse of
# the API's READABLE_PATHS: that one backs a machine-readable passthrough with
# a fixed set of callers, and widening it here would widen that too.
READABLE = frozenset(
    {
        "system/resource",
        "system/identity",
        "system/package",
        "system/clock",
        "system/health",
        "system/history",
        "system/routerboard",
        "system/scheduler",
        "system/script",
        "log",
        "interface",
        "interface/bridge",
        "interface/bridge/host",
        "interface/bridge/port",
        "interface/ethernet",
        "interface/eoip",
        "interface/gre",
        "interface/ipip",
        "interface/vlan",
        "interface/vxlan",
        # Not "interface/wireguard" itself: those rows carry private-key next
        # to the public one. Peers carry only public keys, and they are what
        # answers "did the far end ever hand its key over".
        "interface/wireguard/peers",
        "ip/address",
        "ip/arp",
        "ip/dhcp-client",
        "ip/dhcp-server",
        "ip/dhcp-server/lease",
        "ip/dns",
        "ip/dns/cache",
        "ip/firewall/address-list",
        "ip/firewall/connection",
        "ip/firewall/filter",
        "ip/firewall/mangle",
        "ip/firewall/nat",
        "ip/ipsec/active-peers",
        "ip/ipsec/installed-sa",
        "ip/ipsec/peer",
        "ip/ipsec/policy",
        "ip/ipsec/profile",
        "ip/ipsec/proposal",
        "ip/neighbor",
        "ip/route",
        "ip/service",
        "ipv6/address",
        "ipv6/route",
        "queue/simple",
        "routing/bgp/connection",
        "routing/bgp/network",
        "routing/bgp/session",
        "routing/bgp/template",
        "routing/table",
        "tool/netwatch",
    }
)

# Menus deliberately absent from the list above, with the reason. Named rather
# than merely omitted, so the console can say *why* instead of "not allowed" --
# a bare refusal trains people to stop reading the message.
#
# These match the menu *and everything under it*, so a submenu added by a later
# RouterOS is refused by default rather than allowed by oversight.
REFUSED_TREE = {
    "user": "lists the device's accounts",
    "ip/ipsec/identity": "holds pre-shared keys",
    "ip/ipsec/key": "holds private keys",
    "ip/hotspot/user": "holds passwords",
    "ppp/secret": "holds passwords",
    "file": "can be used to read the configuration export, secrets included",
    "export": "prints the whole configuration, secrets included",
}

# Refused on their own, while their submenus stay judged on their own merits.
REFUSED_EXACT = {
    "interface/wireguard": (
        "holds each private key in the same row as its public one. "
        "/interface/wireguard/peers is readable and is usually what you want"
    ),
}

# Commands that do something rather than print something, and are still safe:
# they change no configuration, leave no rows behind, and stop on their own.
ACTIONS = frozenset({"ping", "tool/traceroute"})

# Verbs that only read. "export" is not here on purpose -- it prints the whole
# configuration including secrets, which is the one thing this console must not
# become a way to do.
READ_VERBS = frozenset({"print", "get", "find"})

# Verbs that change the device. Listed rather than inferred so a refusal can
# say *why*: without this, "/ip/route/remove" parses as a menu nobody has
# heard of and the console answers "not on the list", which is true and
# useless.
WRITE_VERBS = frozenset(
    {
        "set", "add", "remove", "enable", "disable", "move", "unset", "reset",
        "comment", "edit", "reboot", "shutdown", "upgrade", "downgrade",
        "reset-configuration", "save", "load", "download", "upload", "run",
        "start", "stop", "cancel", "clear", "import", "make-supout.rif",
    }
)

# A property name in key=value. Values are checked separately.
_KEY = re.compile(r"^[a-z][a-z0-9.-]*$")
_VALUE = re.compile(r"^[A-Za-z0-9 ._:@/%,+=-]{0,200}$")


class ConsoleRefused(ValueError):
    """The command was understood and is not allowed. Carries the reason."""


@dataclass(slots=True)
class ConsoleResult:
    command: str
    # What the console actually did, in a form somebody can check against the
    # allowlist themselves. A console that will not say what it ran is asking
    # to be trusted for no reason.
    resolved: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None


@dataclass(slots=True)
class ParsedCommand:
    path: str
    verb: str
    params: dict[str, str]


def parse(command: str) -> ParsedCommand:
    """Split a RouterOS console line into a path, a verb, and parameters.

    Accepts both spellings people actually type: ``/ip/route/print`` and
    ``/ip route print``. RouterOS accepts both, so refusing one would be the
    console being pedantic about something the device is not.
    """
    text = command.strip()
    if not text:
        raise ConsoleRefused("Type a command, for example /ip/route/print")
    if ";" in text or "\n" in text or "[" in text or "$" in text:
        # No command chaining, no scripting, no variable expansion. Each of
        # those turns one checked command into an arbitrary number of
        # unchecked ones.
        raise ConsoleRefused(
            "One plain command at a time. Semicolons, brackets and $ are not "
            "accepted -- they would turn one checked command into several "
            "unchecked ones."
        )

    try:
        tokens = shlex.split(text)
    except ValueError as exc:
        raise ConsoleRefused(f"Could not read that command: {exc}") from exc

    words: list[str] = []
    params: dict[str, str] = {}
    for token in tokens:
        if "=" in token and not token.startswith("/"):
            key, _, value = token.partition("=")
            key = key.strip().lower()
            if not _KEY.match(key):
                raise ConsoleRefused(f"{key!r} is not a property name")
            if not _VALUE.match(value):
                raise ConsoleRefused(f"the value for {key!r} has characters that are not allowed")
            params[key] = value
        else:
            words.extend(part for part in token.split("/") if part)

    if not words:
        raise ConsoleRefused("Type a command, for example /ip/route/print")

    # Lowered before the verb is picked out, not after. Otherwise "/IP/Route/
    # Print" keeps "Print" in the path, fails the allowlist, and is refused
    # with a message about an unknown menu rather than simply working. Menu
    # words are lowercase in RouterOS; only values carry case, and those were
    # separated out above.
    words = [word.lower() for word in words]

    # The last word is the verb only when it is one; "/ip/route" on its own
    # means print, which is what RouterOS does too.
    if words[-1] in READ_VERBS or words[-1] in WRITE_VERBS or words[-1] in {
        "monitor",
        "export",
    }:
        verb = words[-1]
        path = "/".join(words[:-1])
    else:
        path = "/".join(words)
        verb = "print"

    return ParsedCommand(path=path, verb=verb, params=params)


def check(parsed: ParsedCommand) -> None:
    """Raise ConsoleRefused unless this command is allowed.

    Separate from ``parse`` so the reason can be specific. "Not allowed" with
    no explanation trains people to stop reading the message.
    """
    if parsed.path in ACTIONS:
        return

    if parsed.verb == "export":
        raise ConsoleRefused(
            "export prints the whole configuration, secrets included. Use the "
            "backup and intent export instead, which redact."
        )
    if parsed.verb == "monitor":
        raise ConsoleRefused(
            "monitor streams until it is stopped, and there is nothing here to "
            "stop it. Use print, or the diagnostics page."
        )
    if parsed.verb not in READ_VERBS:
        raise ConsoleRefused(
            f"{parsed.verb!r} changes configuration. This console reads; "
            "changes go through plan and apply, where they leave a diff, a "
            "backup and a rollback behind them. A change made here would be "
            "drift, and the next apply would silently revert it."
        )

    if parsed.path in REFUSED_EXACT:
        raise ConsoleRefused(
            f"/{parsed.path} is not readable here: it {REFUSED_EXACT[parsed.path]}."
        )
    for prefix, why in REFUSED_TREE.items():
        if parsed.path == prefix or parsed.path.startswith(prefix + "/"):
            raise ConsoleRefused(f"/{parsed.path} is not readable here: it {why}.")

    if parsed.path not in READABLE:
        raise ConsoleRefused(
            f"/{parsed.path} is not on the console's list. This is an allowlist, "
            "so a menu that holds credentials cannot be reached by spelling it "
            "differently."
        )


# Properties stripped from every row before it leaves the controller, even on
# an allowed menu. Belt and braces: the allowlist above is the real control,
# and this catches a menu that grows a secret property in a later RouterOS.
_SENSITIVE = frozenset(
    {
        "secret",
        "password",
        "private-key",
        "preshared-key",
        "ipsec-secret",
        "passphrase",
        "key",
    }
)


def precheck(command: str) -> ParsedCommand:
    """Parse and check without a device.

    Exposed so a caller can refuse before opening a connection: a command that
    was never going to be allowed should not cost a TLS handshake with a
    router, and should not appear in that router's own log either.
    """
    parsed = parse(command)
    check(parsed)
    return parsed


async def run_console(driver: DeviceDriver, command: str) -> ConsoleResult:
    """Parse, check, run, redact."""
    parsed = precheck(command)

    if parsed.path in ACTIONS:
        resolved = f"/{parsed.path} " + " ".join(
            f"{k}={v}" for k, v in sorted(parsed.params.items())
        )
        try:
            raw = await driver.run(f"/{parsed.path}", dict(parsed.params))
        except DriverError as exc:
            return ConsoleResult(command=command, resolved=resolved.strip(), error=str(exc))
        rows = raw if isinstance(raw, list) else [raw] if isinstance(raw, dict) else []
    else:
        resolved = f"/{parsed.path}/print" + (
            " " + " ".join(f"{k}={v}" for k, v in sorted(parsed.params.items()))
            if parsed.params
            else ""
        )
        try:
            rows = await driver.read(f"/{parsed.path}", parsed.params or None)
        except DriverError as exc:
            return ConsoleResult(command=command, resolved=resolved, error=str(exc))

    return ConsoleResult(
        command=command,
        resolved=resolved,
        rows=[
            {k: v for k, v in row.items() if k.lower() not in _SENSITIVE}
            for row in rows
            if isinstance(row, dict)
        ],
    )
