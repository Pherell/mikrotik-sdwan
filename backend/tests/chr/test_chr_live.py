"""Live tests against a real RouterOS CHR (plan C1, step 1).

These are the counterpart to ``tests/fakeros``: the fake proves the
driver's request shapes, this module proves the same code path works
against an actual RouterOS. It exercises the repo's ``Ros7RestDriver``
and the reconcile planner -- never raw HTTP -- so a failure here is a
failure the controller itself would hit.

Skipped by default. They only run when ``CHR_LIVE=1`` and a password is
supplied via ``CHR_PASSWORD`` (see ``tests/chr/README.md``), so the
normal suite and CI are unaffected. The target device must be a
throwaway lab CHR: the write test creates and then removes its own
address-list entry, and each test cleans up any ``sdwan:chr-harness``
rows it finds before it starts, in case an earlier run died mid-test.

Scope is deliberately one node: identity, one harmless write round-trip,
and an idempotency probe through the reconcile planner. The hub +
2-spoke topology and the chaos drills from plan C1 are later steps.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

import pytest

from app.drivers.base import ConfigItem, ConfigOp, ConfigSection, OpKind
from app.drivers.ros7_rest import Ros7RestDriver
from app.reconcile.plan import build_plan

pytestmark = pytest.mark.chr

PATH = "/ip/firewall/address-list"
TAG = "sdwan:chr-harness"
LIST_NAME = "sdwan-chr-harness"
# TEST-NET-1 (RFC 5737 documentation prefix): never routed anywhere, so
# the entry is inert even while it exists.
ADDRESS = "192.0.2.99"


def _skip_reason() -> str | None:
    if os.environ.get("CHR_LIVE") != "1":
        return "live CHR tests are opt-in: set CHR_LIVE=1 (see tests/chr/README.md)"
    if not os.environ.get("CHR_PASSWORD"):
        return "CHR_PASSWORD is not set"
    return None


@pytest.fixture
async def chr_driver():
    reason = _skip_reason()
    if reason is not None:
        pytest.skip(reason)

    # The driver builds its httpx client with trust_env on, so a proxy
    # configured in the surrounding environment is picked up. On the
    # machine this harness was developed on, the inherited NO_PROXY
    # contains bracketed IPv6 entries that httpx cannot parse as URL
    # patterns, and client construction fails before any request is
    # made. The CHR is a local lab device: bypass proxies entirely.
    for var in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY",
                "ALL_PROXY", "all_proxy"):
        os.environ.pop(var, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"

    base = os.environ.get("CHR_BASE_URL", "https://127.0.0.1:18443").rstrip("/")
    if base.endswith("/rest"):
        base = base[: -len("/rest")]
    parsed = urlparse(base)
    scheme = parsed.scheme or "https"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if scheme == "https" else 80)

    driver = Ros7RestDriver(
        host,
        os.environ.get("CHR_USER", "admin"),
        os.environ["CHR_PASSWORD"],
        port=port,
        scheme=scheme,
        # The lab CHR presents the self-signed certificate created for
        # the PoC. TOFU pinning (the driver default) still applies:
        # connect() learns the fingerprint on first contact.
        verify_tls=False,
    )
    await driver.connect()
    try:
        yield driver
    finally:
        await driver.close()


def _harness_section(items: list[ConfigItem]) -> ConfigSection:
    return ConfigSection(
        path=PATH,
        items=items,
        owner_tag=TAG,
        key=("list", "address"),
    )


def _harness_item() -> ConfigItem:
    return ConfigItem(props={"list": LIST_NAME, "address": ADDRESS}, tag=TAG)


async def _harness_rows(driver: Ros7RestDriver) -> list[dict]:
    rows = await driver.read(PATH)
    return [r for r in rows if str(r.get("comment", "")).startswith(TAG)]


async def _remove_harness_rows(driver: Ros7RestDriver) -> None:
    for row in await _harness_rows(driver):
        result = await driver.apply(
            [ConfigOp(kind=OpKind.remove, path=PATH, item_id=str(row[".id"]))]
        )
        assert result.ok, result.error


async def test_identity_is_a_real_chr(chr_driver: Ros7RestDriver) -> None:
    rows = await chr_driver.read("/system/resource")
    assert rows, "no /system/resource row"
    assert str(rows[0].get("version", "")).startswith("7.")
    assert "CHR" in str(rows[0].get("board-name", ""))

    caps = await chr_driver.capabilities()
    assert caps.ros_major == 7
    assert caps.version.startswith("7.")
    assert "CHR" in caps.board_name


async def test_address_list_write_round_trip(chr_driver: Ros7RestDriver) -> None:
    await _remove_harness_rows(chr_driver)
    try:
        result = await chr_driver.apply(
            [
                ConfigOp(
                    kind=OpKind.add,
                    path=PATH,
                    props={"list": LIST_NAME, "address": ADDRESS},
                    comment=TAG,
                )
            ]
        )
        assert result.ok, result.error

        rows = await _harness_rows(chr_driver)
        assert len(rows) == 1
        assert rows[0].get("list") == LIST_NAME
        assert rows[0].get("address") in (ADDRESS, f"{ADDRESS}/32")
    finally:
        await _remove_harness_rows(chr_driver)

    assert await _harness_rows(chr_driver) == []


async def test_reconcile_reapply_is_idempotent(chr_driver: Ros7RestDriver) -> None:
    """Applying the same intent twice through build_plan must not duplicate.

    The first plan adds the row; a second plan built from the same intent
    against the now-live device must be empty. Cleanup also goes through
    the planner (an empty section removes the rows it owns), so the whole
    test only uses controller code paths.
    """
    await _remove_harness_rows(chr_driver)
    section = _harness_section([_harness_item()])
    try:
        plan = await build_plan(chr_driver, [section])
        assert plan.counts["add"] == 1
        result = await chr_driver.apply(plan.ops())
        assert result.ok, result.error
        assert len(await _harness_rows(chr_driver)) == 1

        second = await build_plan(chr_driver, [section])
        assert second.empty, second.render()
        assert len(await _harness_rows(chr_driver)) == 1
    finally:
        cleanup = await build_plan(chr_driver, [_harness_section([])])
        if not cleanup.empty:
            result = await chr_driver.apply(cleanup.ops())
            assert result.ok, result.error

    assert await _harness_rows(chr_driver) == []
