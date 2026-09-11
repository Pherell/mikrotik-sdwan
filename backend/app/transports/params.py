"""What a transport lets you change, and what a valid value looks like.

The mechanism was already there: `Fabric.transport_params` is merged over each
transport's defaults at render time. What was missing is that the field was an
untyped `dict`, which fails in the two ways free-form configuration always
fails.

A misspelled key is *silently ignored* -- write `dh_grup` and the fabric builds
with the default, exactly as if you had set nothing, and nothing anywhere says
so. A bad value is worse: it renders, applies cleanly, and the tunnel never
establishes, because IKE mismatches do not report themselves as configuration
errors. Both failures look like "the VPN is broken" days later.

So each transport declares its options here, with the allowed values and a
sentence saying what the option is for. That gives validation, and it gives the
UI something to draw without a second copy of the same list going stale.

**Everything here must match on both ends.** These are negotiated parameters:
one side offering aes-256-gcm and the other aes-128-cbc is not a weaker tunnel,
it is no tunnel. The controller renders both ends from one fabric, so it is
consistent by construction -- but only for ends it renders. A device configured
by hand is on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Option:
    """One knob, its allowed values, and why anyone would touch it."""

    key: str
    label: str
    why: str
    # Empty means "any value of the right shape", checked by kind below.
    choices: tuple[str, ...] = ()
    kind: str = "choice"  # choice | int | duration | text
    minimum: int | None = None
    maximum: int | None = None


# RouterOS duration: a number and a unit, possibly repeated. "8h", "1d12h",
# "30s". Checked loosely on purpose -- the device is the authority on what it
# accepts, and a stricter rule here would reject values that work.
_DURATION_UNITS = ("s", "m", "h", "d", "w")


def _is_duration(value: str) -> bool:
    text = value.strip().lower()
    if not text or not text[0].isdigit():
        return False
    seen_digit = False
    for char in text:
        if char.isdigit():
            seen_digit = True
        elif char in _DURATION_UNITS:
            if not seen_digit:
                return False
            seen_digit = False
        else:
            return False
    # A trailing digit with no unit ("8") is not a duration RouterOS accepts.
    return not seen_digit


IPSEC_OPTIONS: tuple[Option, ...] = (
    Option(
        key="mode",
        label="Mode",
        why=(
            "full builds the whole IPsec stack. simple collapses it to GRE with "
            "an ipsec-secret, which is fewer moving parts and fewer options."
        ),
        choices=("full", "simple"),
    ),
    Option(
        key="enc_algorithm",
        label="Encryption",
        why=(
            "GCM combines encryption and authentication and is faster where the "
            "hardware supports it. CBC is what an older RouterBOARD may need."
        ),
        choices=(
            "aes-256-gcm",
            "aes-128-gcm",
            "aes-256-cbc",
            "aes-192-cbc",
            "aes-128-cbc",
            "3des",
        ),
    ),
    Option(
        key="auth_algorithm",
        label="Authentication",
        why=(
            "Ignored when the encryption is GCM, which authenticates already. "
            "Required with CBC."
        ),
        choices=("sha256", "sha512", "sha1", "md5"),
    ),
    Option(
        key="dh_group",
        label="Key exchange group",
        why=(
            "The Diffie-Hellman group for phase 1. The elliptic-curve groups are "
            "faster and smaller than the modp ones at equivalent strength."
        ),
        choices=(
            "ecp256",
            "ecp384",
            "ecp521",
            "modp2048",
            "modp3072",
            "modp4096",
            "modp1024",
        ),
    ),
    Option(
        key="pfs_group",
        label="Perfect forward secrecy group",
        why=(
            "Re-keys phase 2 independently, so a compromised key does not open "
            "past traffic. 'none' turns it off, which is faster and worse."
        ),
        choices=(
            "ecp256",
            "ecp384",
            "ecp521",
            "modp2048",
            "modp3072",
            "modp4096",
            "modp1024",
            "none",
        ),
    ),
    Option(
        key="lifetime",
        label="Key lifetime",
        why=(
            "How long before keys are renegotiated. Shorter limits the damage "
            "of a compromised key; too short and the tunnel spends its time "
            "rekeying."
        ),
        kind="duration",
    ),
    Option(
        key="exchange_mode",
        label="IKE version",
        why=(
            "ike2 unless something at the far end only speaks the old one. "
            "Both ends must agree; there is no negotiation between versions."
        ),
        choices=("ike2", "main", "aggressive"),
    ),
    Option(
        key="dpd_interval",
        label="Dead peer detection interval",
        why="How often to check the far end is still there.",
        kind="duration",
    ),
    Option(
        key="dpd_maximum_failures",
        label="Dead peer detection failures",
        why=(
            "How many missed checks before the peer is declared dead. Times the "
            "interval, this is how long a dead tunnel stays up."
        ),
        kind="int",
        minimum=1,
        maximum=100,
    ),
)

L2_OPTIONS: tuple[Option, ...] = (
    Option(
        key="bridge",
        label="Bridge",
        why=(
            "The bridge each stretched segment lands on. The controller manages "
            "the tunnel; putting local ports into this bridge is yours."
        ),
        kind="text",
    ),
    Option(
        key="vni",
        label="Tunnel ID",
        why="Must be the same at both ends and different from every other segment.",
        kind="int",
        minimum=1,
        maximum=16_777_215,
    ),
    Option(
        key="vxlan_port",
        label="VXLAN port",
        why="8472 is the Linux default; 4789 is the IANA one. Both ends must agree.",
        kind="int",
        minimum=1,
        maximum=65535,
    ),
)

WIREGUARD_OPTIONS: tuple[Option, ...] = (
    Option(
        key="listen_port",
        label="First listen port",
        why=(
            "Where this fabric's ports start. Each tunnel takes its own port "
            "from here upwards, because one WireGuard interface is one UDP "
            "listener and a site with two uplinks has two. Move it only if "
            "something in the path blocks the default."
        ),
        kind="int",
        minimum=1,
        maximum=65535,
    ),
    Option(
        key="persistent_keepalive",
        label="Keepalive",
        why=(
            "How often a peer behind NAT pokes the far end to keep its mapping "
            "alive. Without it an inbound tunnel to a NAT'd site dies the "
            "moment the mapping expires."
        ),
        kind="duration",
    ),
)

# Keyed by transport name, matching TransportDriver.name.
OPTIONS: dict[str, tuple[Option, ...]] = {
    "ipsec_gre": IPSEC_OPTIONS,
    "ipsec_policy": IPSEC_OPTIONS,
    "vxlan": L2_OPTIONS,
    "eoip": L2_OPTIONS,
    # GRE and IPIP have nothing to negotiate: no ciphers to agree on, and no
    # port. An empty tuple is the honest answer, and the UI draws "nothing to
    # configure" rather than an empty Advanced section that looks broken.
    "gre": (),
    "ipip": (),
    # WireGuard's ciphers are not selectable by design, so the port is the only
    # thing left -- and it is worth exposing, because the one reason a
    # WireGuard fabric fails to come up is something in the path dropping that
    # port.
    "wireguard": WIREGUARD_OPTIONS,
}


@dataclass(slots=True)
class ParamError:
    key: str
    message: str


def options_for(transport: str) -> tuple[Option, ...]:
    return OPTIONS.get(transport, ())


def validate(transport: str, params: dict[str, Any]) -> list[ParamError]:
    """Check a fabric's overrides against what its transport accepts.

    Returns every problem rather than the first, because someone pasting a
    security standard in is likely to have several and fixing them one round
    trip at a time is miserable.
    """
    allowed = {option.key: option for option in options_for(transport)}
    errors: list[ParamError] = []

    for key, value in params.items():
        option = allowed.get(key)
        if option is None:
            # The failure this whole module exists for. Silently ignoring an
            # unknown key means the fabric builds with the default and nothing
            # says the setting did not take.
            known = ", ".join(sorted(allowed)) or "nothing"
            errors.append(
                ParamError(
                    key,
                    f"{key!r} is not a setting for the {transport} transport. "
                    f"It accepts: {known}.",
                )
            )
            continue

        text = str(value).strip()
        if option.kind == "choice":
            if text not in option.choices:
                errors.append(
                    ParamError(
                        key,
                        f"{text!r} is not one of: {', '.join(option.choices)}.",
                    )
                )
        elif option.kind == "duration":
            if not _is_duration(text):
                errors.append(
                    ParamError(
                        key,
                        f"{text!r} is not a duration. Use a number and a unit, "
                        "such as 8h, 30m or 1d.",
                    )
                )
        elif option.kind == "int":
            try:
                number = int(text)
            except ValueError:
                errors.append(ParamError(key, f"{text!r} is not a whole number."))
                continue
            if option.minimum is not None and number < option.minimum:
                errors.append(ParamError(key, f"must be at least {option.minimum}."))
            if option.maximum is not None and number > option.maximum:
                errors.append(ParamError(key, f"must be at most {option.maximum}."))
        elif option.kind == "text":
            if not text:
                errors.append(ParamError(key, "cannot be empty."))
            elif len(text) > 64:
                errors.append(ParamError(key, "is too long for a RouterOS name."))

    return errors


@dataclass(slots=True)
class TransportOptions:
    """What the UI needs to draw one transport's Advanced section."""

    transport: str
    options: list[dict[str, Any]] = field(default_factory=list)


def describe(transport: str, defaults: dict[str, Any]) -> TransportOptions:
    """Options plus their defaults, for the UI.

    The defaults come from the transport module rather than being repeated
    here: two lists of defaults is one list of defaults and one lie.
    """
    return TransportOptions(
        transport=transport,
        options=[
            {
                "key": option.key,
                "label": option.label,
                "why": option.why,
                "kind": option.kind,
                "choices": list(option.choices),
                "minimum": option.minimum,
                "maximum": option.maximum,
                "default": defaults.get(option.key),
            }
            for option in options_for(transport)
        ],
    )
