/** IPv4 prefix arithmetic, for catching a steering rule that swallows the
 * infrastructure it rides on.
 *
 * A policy prefix that covers a peer's WAN address or the tunnel overlay pool
 * steers the fabric's own traffic into the fabric. Measured on hardware with
 * dst=10.0.0.0/8 on a hub whose spoke sits at 10.1.11.229: ten pings turned
 * into 1,221 packets looping between the two routers, the hub's own LAN lost
 * 80% of its traffic to the far site, and reaching a host on the hub's uplink
 * subnet went from 0.35ms direct to 1.37ms and two hops.
 *
 * Comparing prefix strings cannot see any of that -- 192.168.0.0/16 and
 * 10.1.0.0/16 are as dangerous as 10.0.0.0/8 and look nothing like it.
 */

export type Prefix = { base: number; bits: number };

function ipToInt(text: string): number | null {
  const parts = text.split(".");
  if (parts.length !== 4) return null;
  let value = 0;
  for (const part of parts) {
    if (!/^\d{1,3}$/.test(part)) return null;
    const octet = Number(part);
    if (octet > 255) return null;
    value = value * 256 + octet;
  }
  return value;
}

function maskOf(bits: number): number {
  return bits === 0 ? 0 : (0xffffffff << (32 - bits)) >>> 0;
}

/** "10.1.11.229/24" -> the /24 it sits in. A bare address is a /32. */
export function parsePrefix(text: string): Prefix | null {
  const [address, maskText] = text.trim().split("/");
  const ip = ipToInt(address ?? "");
  if (ip === null) return null;
  const bits = maskText === undefined || maskText === "" ? 32 : Number(maskText);
  if (!Number.isInteger(bits) || bits < 0 || bits > 32) return null;
  return { base: (ip & maskOf(bits)) >>> 0, bits };
}

/** Does `outer` contain every address in `inner`? */
export function covers(outer: Prefix, inner: Prefix): boolean {
  if (outer.bits > inner.bits) return false;
  return ((inner.base & maskOf(outer.bits)) >>> 0) === outer.base;
}

export type Infra = { cidr: string; what: string };

/** Which pieces of infrastructure this one policy prefix would swallow. */
export function swallowed(prefixText: string, infra: Infra[]): Infra[] {
  const outer = parsePrefix(prefixText);
  if (outer === null) return [];
  return infra.filter((entry) => {
    const inner = parsePrefix(entry.cidr);
    return inner !== null && covers(outer, inner);
  });
}
