/**
 * Everything about talking to this controller from something that is not a
 * browser: the credentials, how to use them, and the reference.
 *
 * The three belong together. A tokens page with no tutorial produces a
 * credential nobody knows what to do with; a tutorial with no tokens page
 * tells people to use their login JWT, which is exactly the habit tokens
 * exist to replace.
 */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { PageHeader } from "../components/PageHeader";
import { Skeleton } from "../components/Skeleton";
import { useToast } from "../components/Toaster";
import { endpoints, type ApiToken } from "../lib/api";

type Tab = "tokens" | "tutorial" | "reference";

export function ApiPage() {
  const [tab, setTab] = useState<Tab>("tokens");

  return (
    <>
      <PageHeader
        title="API"
        description="Everything needed to drive this controller from a script: a credential that is not a person, how to use it, and the full reference."
      />

      <div className="tabs">
        <button
          className={`tab${tab === "tokens" ? " active" : ""}`}
          onClick={() => setTab("tokens")}
        >
          Tokens
        </button>
        <button
          className={`tab${tab === "tutorial" ? " active" : ""}`}
          onClick={() => setTab("tutorial")}
        >
          Getting started
        </button>
        <button
          className={`tab${tab === "reference" ? " active" : ""}`}
          onClick={() => setTab("reference")}
        >
          Reference
        </button>
      </div>

      {tab === "tokens" && <TokensPanel />}
      {tab === "tutorial" && <TutorialPanel />}
      {tab === "reference" && <ReferencePanel />}
    </>
  );
}

// -- tokens -----------------------------------------------------------------

function TokensPanel() {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [minted, setMinted] = useState<string | null>(null);

  const tokens = useQuery({ queryKey: ["api-tokens"], queryFn: endpoints.apiTokens });

  const revoke = useMutation({
    mutationFn: (id: string) => endpoints.revokeApiToken(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["api-tokens"] });
      toast.ok("Token revoked. Anything using it stops working now.");
    },
    onError: (e) => toast.bad((e as Error).message),
  });

  return (
    <>
      <NewToken
        onMinted={(credential) => {
          setMinted(credential);
          queryClient.invalidateQueries({ queryKey: ["api-tokens"] });
        }}
      />

      {minted && <MintedOnce credential={minted} onDismiss={() => setMinted(null)} />}

      <div className="card">
        <h2>Tokens</h2>
        {tokens.isLoading && <Skeleton rows={3} />}
        {tokens.isError && <div className="error">{(tokens.error as Error).message}</div>}

        {tokens.data?.length === 0 && (
          <p className="muted">
            No tokens yet. Until there is one, every script has to sign in as a
            person — which means it runs with that person's full rights, stops
            working when their password changes, and cannot be turned off
            without disabling them.
          </p>
        )}

        {tokens.data && tokens.data.length > 0 && (
          <table className="stack">
            <thead>
              <tr>
                <th>Name</th>
                <th>Prefix</th>
                <th>Can do</th>
                <th>Expires</th>
                <th>Last used</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {tokens.data.map((token) => (
                <TokenRow
                  key={token.id}
                  token={token}
                  onRevoke={() => revoke.mutate(token.id)}
                  busy={revoke.isPending}
                />
              ))}
            </tbody>
          </table>
        )}
      </div>
    </>
  );
}

function TokenRow({
  token,
  onRevoke,
  busy,
}: {
  token: ApiToken;
  onRevoke: () => void;
  busy: boolean;
}) {
  const revoked = token.revoked_at !== null;
  const expired =
    token.expires_at !== null && new Date(token.expires_at) <= new Date();

  return (
    <tr>
      <td data-label="Name">
        <strong>{token.name}</strong>
        {revoked && <div className="muted">revoked</div>}
      </td>
      <td data-label="Prefix">
        <code>{token.prefix}</code>
      </td>
      <td data-label="Can do">
        <span className="badge">{ROLE_BLURB[token.role]}</span>
      </td>
      <td data-label="Expires" className="muted">
        {token.expires_at === null ? (
          "never"
        ) : expired ? (
          <span className="badge unreachable">expired</span>
        ) : (
          new Date(token.expires_at).toLocaleDateString()
        )}
      </td>
      <td data-label="Last used" className="muted">
        {/* The question you ask before revoking something. */}
        {token.last_used_at
          ? new Date(token.last_used_at).toLocaleString()
          : "never used"}
      </td>
      <td data-label="">
        {!revoked && (
          <button className="sm danger" onClick={onRevoke} disabled={busy}>
            Revoke
          </button>
        )}
      </td>
    </tr>
  );
}

const ROLE_BLURB: Record<ApiToken["role"], string> = {
  viewer: "read only",
  operator: "read and apply configuration",
  admin: "everything, including people and credentials",
};

function NewToken({ onMinted }: { onMinted: (credential: string) => void }) {
  const toast = useToast();
  const [name, setName] = useState("");
  const [role, setRole] = useState("viewer");
  const [days, setDays] = useState("90");

  const create = useMutation({
    mutationFn: () =>
      endpoints.createApiToken({
        name,
        role,
        expires_in_days: days === "never" ? null : Number(days),
      }),
    onSuccess: (result) => {
      setName("");
      onMinted(result.token);
    },
    onError: (e) => toast.bad((e as Error).message),
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <div className="card">
      <h2>New token</h2>
      <form onSubmit={submit}>
        <div className="row">
          <label>
            Name
            <input
              required
              placeholder="ci-deploy"
              value={name}
              onChange={(e) => setName(e.target.value)}
            />
            <span className="muted field-note">
              What is using it. This is the name that turns up in the audit
              trail next to everything the token does.
            </span>
          </label>
          <label>
            Can do
            <select value={role} onChange={(e) => setRole(e.target.value)}>
              <option value="viewer">read only</option>
              <option value="operator">read and apply configuration</option>
              <option value="admin">everything</option>
            </select>
            <span className="muted field-note">
              Never more than your own account can do. Demote the account and
              its tokens weaken with it.
            </span>
          </label>
          <label>
            Expires
            <select value={days} onChange={(e) => setDays(e.target.value)}>
              <option value="30">in 30 days</option>
              <option value="90">in 90 days</option>
              <option value="365">in a year</option>
              <option value="never">never</option>
            </select>
            <span className="muted field-note">
              "Never" is allowed. A token that dies unannounced at 3am is its
              own kind of outage.
            </span>
          </label>
        </div>
        <div className="row" style={{ justifyContent: "flex-start" }}>
          <div className="no-grow">
            <button
              className="primary"
              type="submit"
              data-busy={create.isPending}
              disabled={create.isPending || name === ""}
            >
              {create.isPending ? "Minting…" : "Create token"}
            </button>
          </div>
        </div>
      </form>
    </div>
  );
}

/**
 * Shown once. The server does not store the usable value, so there is no
 * second chance and the panel says so rather than letting someone discover it.
 */
function MintedOnce({
  credential,
  onDismiss,
}: {
  credential: string;
  onDismiss: () => void;
}) {
  const toast = useToast();
  const [copied, setCopied] = useState(false);

  async function copy() {
    try {
      await navigator.clipboard.writeText(credential);
      setCopied(true);
    } catch {
      // A page served over plain HTTP has no clipboard API. Selecting the
      // text still works, so this is a note rather than a failure.
      toast.bad("Could not copy. Select the token and copy it by hand.");
    }
  }

  return (
    <div className="card">
      <h2>Copy this now</h2>
      <div className="warn">
        This is the only time this token is shown. It is not stored in a form
        anyone can read back — if you lose it, revoke it and make another.
      </div>
      <pre className="token-reveal">{credential}</pre>
      <div className="row" style={{ justifyContent: "flex-start" }}>
        <div className="no-grow">
          <button className="primary" onClick={copy}>
            {copied ? "Copied" : "Copy"}
          </button>
        </div>
        <div className="no-grow">
          <button onClick={onDismiss}>I have it</button>
        </div>
      </div>
    </div>
  );
}

// -- tutorial ---------------------------------------------------------------

function TutorialPanel() {
  // Whatever origin the UI is on is the origin the API is on: one Caddy site
  // serves both halves. Reading it from the browser means the examples are
  // copy-pasteable rather than aspirational.
  const base = `${window.location.origin}/api/v1`;

  return (
    <>
      <div className="card">
        <h2>Authenticate</h2>
        <p>
          Every request carries a bearer credential. Use an API token from the
          Tokens tab — not your login, which expires with your session and
          carries all of your rights.
        </p>
        <Snippet
          code={`curl -H "Authorization: Bearer sdwan_..." \\
  ${base}/sites`}
        />
        <p className="muted">
          A token that has expired, been revoked, or never existed all produce
          the same <code>401 Invalid API token</code>. That is deliberate: which
          one it is would be a fact about this installation.
        </p>
      </div>

      <div className="card">
        <h2>Read what is configured</h2>
        <p>
          A <strong>read only</strong> token is enough for everything in this
          section, and it is the right choice for monitoring.
        </p>
        <Snippet
          code={`# every device, with its uplinks
curl -H "$AUTH" ${base}/sites

# every tunnel one device has an end of, and why each is up or down
curl -H "$AUTH" ${base}/sites/$SITE_ID/tunnels

# the router's own log, newest first
curl -H "$AUTH" "${base}/sites/$SITE_ID/log?topic=error"`}
        />
      </div>

      <div className="card">
        <h2>See what a change would do</h2>
        <p>
          <code>plan</code> renders the intended configuration, diffs it against
          the device, and changes nothing. It is a read, so a read-only token
          can run it — which makes it safe to put in a pipeline that is not
          allowed to deploy.
        </p>
        <Snippet code={`curl -X POST -H "$AUTH" ${base}/sites/$SITE_ID/plan`} />
      </div>

      <div className="card">
        <h2>Apply it</h2>
        <p>
          Needs a token that can <strong>apply configuration</strong>.{" "}
          <code>confirm</code> is required and has no default: an apply that
          breaks management access is recovered by the router restoring its
          pre-apply backup, which reboots it.
        </p>
        <Snippet
          code={`curl -X POST -H "$AUTH" -H "Content-Type: application/json" \\
  -d '{"confirm": true}' \\
  ${base}/sites/$SITE_ID/apply`}
        />
        <p className="muted">
          Pass <code>{`{"dry_run": true}`}</code> instead to get the same
          response shape without touching the device.
        </p>
      </div>

      <div className="card">
        <h2>Test from a device</h2>
        <Snippet
          code={`curl -X POST -H "$AUTH" -H "Content-Type: application/json" \\
  -d '{"target": "8.8.8.8", "count": 5, "interface": "ether1"}' \\
  ${base}/sites/$SITE_ID/diagnostics/ping`}
        />
        <p className="muted">
          Naming an interface is how you tell "the internet is down" from "this
          one uplink is down".
        </p>
      </div>

      <div className="card">
        <h2>What each permission means</h2>
        <table className="stack">
          <thead>
            <tr>
              <th>Can do</th>
              <th>Includes</th>
              <th>Good for</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <td data-label="Can do">
                <span className="badge">read only</span>
              </td>
              <td data-label="Includes">
                Everything above except apply. Diagnostics that only read are
                included; ping and traceroute are not.
              </td>
              <td data-label="Good for">Monitoring, dashboards, alerting.</td>
            </tr>
            <tr>
              <td data-label="Can do">
                <span className="badge">read and apply</span>
              </td>
              <td data-label="Includes">
                Push configuration, run diagnostics, read device logs.
              </td>
              <td data-label="Good for">Deployment pipelines.</td>
            </tr>
            <tr>
              <td data-label="Can do">
                <span className="badge">everything</span>
              </td>
              <td data-label="Includes">
                People, credentials, the audit trail. Cannot mint further
                tokens — that stays something a person does.
              </td>
              <td data-label="Good for">
                Very little. Prefer the narrowest that works.
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </>
  );
}

function Snippet({ code }: { code: string }) {
  return <pre className="snippet">{code}</pre>;
}

// -- reference --------------------------------------------------------------

function ReferencePanel() {
  const href = `${window.location.origin}/api/v1/docs`;
  return (
    <div className="card">
      <h2>Full reference</h2>
      <p>
        Every endpoint, every field, and a form to try each one against this
        controller with your own session.
      </p>
      <p>
        <a className="button" href={href} target="_blank" rel="noreferrer">
          Open the API reference
        </a>
      </p>
      <p className="muted">
        The machine-readable schema is at{" "}
        <code>{window.location.origin}/api/v1/openapi.json</code> — point a
        client generator at that rather than writing request shapes by hand.
      </p>
    </div>
  );
}
