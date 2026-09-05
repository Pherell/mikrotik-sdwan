# Security

## Reporting a vulnerability

Use GitHub's **[Report a vulnerability](https://github.com/Pherell/mikrotik-sdwan/security/advisories/new)**
button rather than opening a public issue. If that is unavailable, open an issue
saying only that you have a security report and asking for a contact — no
details.

Expect an acknowledgement within a week. This is not a funded project and there
is no bounty.

## What this software is

A controller that stores, and can decrypt, the administrative credentials of
every router it manages. Compromise of the controller is compromise of the
entire fleet. Deploy it accordingly:

- on a management network, not the internet;
- with `SDWAN_SECRET_KEY` backed up somewhere you would back up a root password —
  losing it makes every stored credential permanently undecryptable;
- with device accounts restricted to the controller's address
  (`/user add ... address=<controller-ip>/32`).

## Known limitations

Stated plainly because a security page that lists only strengths is worthless.

### Not yet verified on hardware

The test suite runs against a fake RouterOS device. **No part of this has been
run against a real MikroTik.** The dead-man rollback in particular — the
mechanism that recovers a router after a bad push — has never fired on real
hardware. Rehearse it in a lab before trusting it on a production edge.
See [docs/verification.md](docs/verification.md).

### Trust on first use, not certificate validation

RouterOS ships a self-signed certificate and an unmanaged SSH host key, so
`verify_tls` defaults to off and chain validation is unavailable. Instead, the
device's identity is **pinned** on first contact and enforced afterwards
(`SDWAN_PIN_DEVICE_IDENTITY`, on by default).

That leaves one exposure: **the first connection is trusted blindly.** Onboard
devices over a network you trust. A later mismatch is refused with a 409 and
must be cleared deliberately.

If you run your own CA, sign the device certificates, set `verify_tls: true` on
the site, and mount the CA into the API container. That is strictly better.

### The controller can decrypt every credential

Inherent to being agentless — it must present the password to reach the device.
Mitigated by running the API container as a non-root user and encrypting at
rest with a PBKDF2-derived key, but a compromise of the API process is a
compromise of the fleet. There is no way around this short of an on-device
agent holding its own keys.

### Tokens cannot be revoked

Signing out is client-side. A stolen JWT is valid until it expires
(`SDWAN_ACCESS_TOKEN_TTL_MINUTES`, 12 hours by default). Shorten the TTL if that
matters more to you than re-authentication frequency; rotating
`SDWAN_JWT_SECRET` invalidates every token at once.

### Backups on the device are unencrypted

Each apply writes a pre-apply backup to the router's own filesystem so the
rollback can restore it. These contain the full configuration in the clear.
The two most recent are kept and older ones are pruned, but anyone with
filesystem access to the router can read them — as they could read the running
configuration anyway.

### Metrics are unauthenticated

`/api/v1/metrics` needs no token. It carries counts and states only — never
names, addresses or topology — and there is a test asserting that. Firewall it
anyway if your monitoring network is not trusted.

## What is enforced

| | |
|---|---|
| Device identity | Pinned on first contact; a mismatch refuses to connect |
| Credentials at rest | Fernet, key derived with PBKDF2-HMAC-SHA256 (480k iterations) |
| Passwords | bcrypt, cost 12; over-length input rejected rather than truncated |
| Login | Locked out after 5 failures per account+source, for 5 minutes |
| Secrets in output | Redacted before any log, job record, or API response |
| Device reads | Allowlisted menus only; secret properties stripped |
| Config changes | Only rows tagged `sdwan:` are ever touched |
| Pushes | Dead-man rollback restores the device if management access is lost |
| Audit | Every state change recorded, including failed logins and lockouts |
| API container | Runs as a non-root user |

## Reviewing it yourself

```bash
make install && make test    # 294 tests, no hardware needed
```

`backend/tests/test_security.py` covers pinning, throttling and key derivation,
and each test names the attack it prevents.
