/**
 * Steering presets.
 *
 * The policy form asks eight questions, three of which are SLA thresholds in
 * milliseconds. That is the right amount of control and the wrong place to
 * start: someone who knows their traffic is voice should not have to decide
 * what jitter budget a call has.
 *
 * A preset answers those questions with a defensible default and says out loud
 * what it chose, so the form stays editable afterwards rather than becoming a
 * black box. Choosing one is optional -- "Start from scratch" is still there.
 */

export interface PolicyPreset {
  id: string;
  title: string;
  /** What this is for, in the user's terms, not the network's. */
  blurb: string;
  /** Why the thresholds are what they are. Shown once chosen. */
  reasoning: string;
  policy: {
    priority: number;
    protocol: string;
    dst_ports: string;
    fallback: string;
  };
  /** Created on demand if a profile of this name does not exist yet. */
  sla: {
    name: string;
    loss_percent: number;
    latency_ms: number;
    jitter_ms: number | null;
    probe_interval_seconds: number;
    probe_count: number;
    recovery_seconds: number;
  } | null;
}

export const POLICY_PRESETS: PolicyPreset[] = [
  {
    id: "voice",
    title: "Voice and video calls",
    blurb: "Teams, Zoom, SIP — anything where a person is listening in real time.",
    reasoning:
      "A call falls apart at loss and jitter levels a download would not notice, so this " +
      "probes twice as often and leaves the link on the first sign of trouble. It is the " +
      "preset most likely to fail over on a link that still looks fine in a speed test.",
    policy: { priority: 10, protocol: "udp", dst_ports: "", fallback: "any" },
    sla: {
      name: "voice",
      loss_percent: 1,
      latency_ms: 150,
      jitter_ms: 30,
      probe_interval_seconds: 5,
      probe_count: 10,
      recovery_seconds: 60,
    },
  },
  {
    id: "interactive",
    title: "Interactive apps and SaaS",
    blurb: "Microsoft 365, Salesforce, web apps — someone is waiting for the page.",
    reasoning:
      "Tolerates a little loss, because TCP recovers from it, but not latency, because a " +
      "person is watching a spinner. Defaults to TCP 443; narrow it with an application " +
      "group if you have one.",
    policy: { priority: 50, protocol: "tcp", dst_ports: "443", fallback: "any" },
    sla: {
      name: "interactive",
      loss_percent: 2,
      latency_ms: 250,
      jitter_ms: 60,
      probe_interval_seconds: 10,
      probe_count: 10,
      recovery_seconds: 60,
    },
  },
  {
    id: "bulk",
    title: "Backups and bulk transfer",
    blurb: "Replication, backups, updates — moves a lot, nobody is watching it.",
    reasoning:
      "Put this on the cheap or metered link and leave it there. The thresholds are loose " +
      "on purpose: moving a backup onto the expensive link because latency rose is a bill, " +
      "not a fix.",
    policy: { priority: 200, protocol: "", dst_ports: "", fallback: "any" },
    sla: {
      name: "bulk",
      loss_percent: 15,
      latency_ms: 800,
      jitter_ms: null,
      probe_interval_seconds: 30,
      probe_count: 10,
      recovery_seconds: 120,
    },
  },
  {
    id: "pinned",
    title: "Keep on one link unless it dies",
    blurb: "Traffic that must not move around — licensing, source-IP allowlists.",
    reasoning:
      "Fails over only when the link is genuinely gone, not when it is merely poor. Use it " +
      "where the far end cares which address you arrive from, since a mid-session failover " +
      "breaks that.",
    policy: { priority: 100, protocol: "", dst_ports: "", fallback: "any" },
    sla: {
      name: "hard-down-only",
      loss_percent: 40,
      latency_ms: 2000,
      jitter_ms: null,
      probe_interval_seconds: 10,
      probe_count: 10,
      recovery_seconds: 30,
    },
  },
];
