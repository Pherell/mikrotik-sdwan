"""Merge rendered sections that target the same RouterOS menu.

Several renderers write to one menu: the site baseline puts a loopback in
``/ip/address`` and every link puts a tunnel address there too. Diffing them
separately is wrong in a way that only shows up later:

* each section filters the device by its *own* ownership tag, so rows belonging
  to a link that no longer exists match no section at all and are never cleaned
  up -- a removed site leaves its tunnels running forever;
* the same menu gets read once per section, multiplying device round-trips.

Merging unions the items and widens the ownership tag to the shared scope, so
one diff covers everything the controller owns in that menu.
"""

from __future__ import annotations

from app.drivers.base import OWNER_PREFIX, ConfigSection


def common_scope(tags: list[str]) -> str:
    """The deepest ownership prefix shared by every tag.

    Segment-wise, never mid-token: ``sdwan:fabric:core:a`` and
    ``sdwan:site:b`` share ``sdwan:``, not ``sdwan:``-plus-a-stray-letter.
    """
    if not tags:
        return OWNER_PREFIX
    split = [t.split(":") for t in tags]
    shared: list[str] = []
    for parts in zip(*split, strict=False):
        if len({*parts}) != 1:
            break
        shared.append(parts[0])
    # Always keep the trailing separator so the prefix cannot match a sibling
    # scope by accident (sdwan:site would otherwise match sdwan:sitewide).
    return ":".join(shared) + ":" if shared else OWNER_PREFIX


def merge_sections(sections: list[ConfigSection]) -> list[ConfigSection]:
    """One section per path, ordered by the lowest apply order of its inputs."""
    merged: dict[str, ConfigSection] = {}

    for section in sections:
        existing = merged.get(section.path)
        if existing is None:
            merged[section.path] = ConfigSection(
                path=section.path,
                items=list(section.items),
                owner_tag=section.owner_tag,
                key=section.key,
                write_once=section.write_once,
                ignore=section.ignore,
                ordered=section.ordered,
                order=section.order,
            )
            continue

        if existing.key != section.key:
            raise ValueError(
                f"Renderers disagree on the identity columns for {section.path}: "
                f"{existing.key} vs {section.key}. Both must name the same "
                "properties or rows cannot be matched."
            )

        existing.items.extend(section.items)
        existing.owner_tag = common_scope([existing.owner_tag, section.owner_tag])
        existing.write_once = tuple(sorted({*existing.write_once, *section.write_once}))
        existing.ignore = tuple(sorted({*existing.ignore, *section.ignore}))
        existing.ordered = existing.ordered or section.ordered
        # Apply at the earliest point any contributor asked for: a dependency
        # is satisfied by being early, never by being late.
        existing.order = min(existing.order, section.order)

    _check_unique_identities(merged.values())
    return sorted(merged.values(), key=lambda s: (s.order, s.path))


def _check_unique_identities(sections: object) -> None:
    """Two renderers producing the same row is a bug that would otherwise show
    up as a device flapping between two intents on alternate applies."""
    for section in sections:  # type: ignore[attr-defined]
        seen: dict[tuple, str] = {}
        for item in section.items:
            identity = item.identity(section.key)
            if identity in seen:
                raise ValueError(
                    f"Two renderers both claim {section.path} {identity}: "
                    f"{seen[identity]} and {item.tag}."
                )
            seen[identity] = item.tag
