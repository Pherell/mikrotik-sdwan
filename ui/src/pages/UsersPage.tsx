import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { type FormEvent, useState } from "react";

import { endpoints, type User } from "../lib/api";

const ROLE_HELP: Record<User["role"], string> = {
  viewer: "Reads everything and can run plan and export. Never changes a device.",
  operator: "Plus apply, expand, drift checks, and policy edits.",
  admin: "Plus user management, and deleting sites and fabrics.",
};

export function UsersPage() {
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);

  const me = useQuery({ queryKey: ["me"], queryFn: endpoints.me });
  const users = useQuery({
    queryKey: ["users"],
    queryFn: endpoints.users,
    retry: false,
  });

  const update = useMutation({
    mutationFn: ({ id, body }: { id: string; body: unknown }) =>
      endpoints.updateUser(id, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["users"] }),
  });

  if (me.data && me.data.role !== "admin") {
    return (
      <div className="card">
        <h2>Users</h2>
        <p className="muted">Only an admin can manage accounts.</p>
      </div>
    );
  }

  return (
    <>
      <div className="card">
        <div className="row" style={{ alignItems: "center" }}>
          <h2 style={{ margin: 0 }}>Users</h2>
          <div style={{ flex: 0 }}>
            <button className="primary" onClick={() => setAdding(true)}>
              New user
            </button>
          </div>
        </div>
        <p className="muted" style={{ marginBottom: 0 }}>
          The bootstrap admin exists for setup. Give people their own accounts with
          the least role that does the job — <code>plan</code> is available to
          viewers precisely so someone can read a diff without being able to push it.
        </p>
      </div>

      {adding && (
        <NewUserForm
          onDone={() => {
            setAdding(false);
            queryClient.invalidateQueries({ queryKey: ["users"] });
          }}
          onCancel={() => setAdding(false)}
        />
      )}

      <div className="card">
        {users.isLoading && <p className="muted">Loading…</p>}
        {users.isError && <div className="error">{(users.error as Error).message}</div>}
        {update.isError && <div className="error">{(update.error as Error).message}</div>}
        {users.data && (
          <table>
            <thead>
              <tr>
                <th>Email</th>
                <th>Name</th>
                <th>Role</th>
                <th>Active</th>
              </tr>
            </thead>
            <tbody>
              {users.data.map((u) => (
                <tr key={u.id}>
                  <td>
                    {u.email}
                    {u.id === me.data?.id && <span className="muted"> · you</span>}
                  </td>
                  <td className="muted">{u.full_name ?? "—"}</td>
                  <td>
                    <select
                      value={u.role}
                      onChange={(e) =>
                        update.mutate({ id: u.id, body: { role: e.target.value } })
                      }
                    >
                      <option value="viewer">viewer</option>
                      <option value="operator">operator</option>
                      <option value="admin">admin</option>
                    </select>
                  </td>
                  <td>
                    <input
                      type="checkbox"
                      style={{ width: "auto" }}
                      checked={u.is_active}
                      onChange={(e) =>
                        update.mutate({
                          id: u.id,
                          body: { is_active: e.target.checked },
                        })
                      }
                    />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      <div className="card">
        <h2>Roles</h2>
        <dl className="kv">
          {(Object.keys(ROLE_HELP) as User["role"][]).map((role) => (
            <div key={role} style={{ display: "contents" }}>
              <dt>{role}</dt>
              <dd>{ROLE_HELP[role]}</dd>
            </div>
          ))}
        </dl>
      </div>
    </>
  );
}

function NewUserForm({ onDone, onCancel }: { onDone: () => void; onCancel: () => void }) {
  const [form, setForm] = useState({
    email: "",
    full_name: "",
    password: "",
    role: "viewer",
  });

  const create = useMutation({
    mutationFn: () =>
      endpoints.createUser({
        email: form.email,
        full_name: form.full_name || null,
        password: form.password,
        role: form.role,
      }),
    onSuccess: onDone,
  });

  function submit(event: FormEvent) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <div className="card">
      <h2>New user</h2>
      {create.isError && <div className="error">{(create.error as Error).message}</div>}
      <form onSubmit={submit}>
        <div className="row">
          <label>
            Email
            <input
              type="email"
              required
              value={form.email}
              onChange={(e) => setForm({ ...form, email: e.target.value })}
            />
          </label>
          <label>
            Full name
            <input
              value={form.full_name}
              onChange={(e) => setForm({ ...form, full_name: e.target.value })}
            />
          </label>
          <label>
            Role
            <select
              value={form.role}
              onChange={(e) => setForm({ ...form, role: e.target.value })}
            >
              <option value="viewer">viewer</option>
              <option value="operator">operator</option>
              <option value="admin">admin</option>
            </select>
          </label>
        </div>
        <label>
          Password
          <input
            type="password"
            required
            minLength={8}
            autoComplete="new-password"
            value={form.password}
            onChange={(e) => setForm({ ...form, password: e.target.value })}
          />
        </label>
        <p className="muted">{ROLE_HELP[form.role as User["role"]]}</p>
        <div className="row" style={{ justifyContent: "flex-start" }}>
          <div style={{ flex: 0 }}>
            <button className="primary" type="submit" disabled={create.isPending}>
              {create.isPending ? "Creating…" : "Create"}
            </button>
          </div>
          <div style={{ flex: 0 }}>
            <button type="button" onClick={onCancel}>
              Cancel
            </button>
          </div>
        </div>
      </form>
    </div>
  );
}
