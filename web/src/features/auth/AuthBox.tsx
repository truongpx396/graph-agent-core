import { type FormEvent, useState } from "react";
import { button, control, cx } from "@/shared/lib/classes";
import { lastOrganization } from "./pkce";
import { useAuth } from "./store";

/** Identity mode only: who is signed in, or where to sign in. It replaces the tenant and principal pickers. */
export function AuthBox() {
  const { who, note, signIn, signOut } = useAuth();
  const [organization, setOrganization] = useState(lastOrganization);
  const submit = (event: FormEvent) => {
    event.preventDefault();
    void signIn(organization);
  };
  return (
    <form id="authBox" className="flex min-w-0 items-center gap-1.5" onSubmit={submit}>
      {who ? (
        <>
          <span id="authWho" className="truncate text-sm font-semibold">
            {who}
          </span>
          <button type="button" id="authSignOut" className={button} onClick={signOut}>
            Sign out
          </button>
        </>
      ) : (
        <>
          <input
            id="authOrg"
            className={cx(control, "w-28")}
            placeholder="organization"
            aria-label="Organization"
            autoComplete="organization"
            spellCheck={false}
            value={organization}
            onChange={(event) => setOrganization(event.target.value)}
          />
          <button type="submit" id="authSignIn" className={button}>
            Sign in
          </button>
        </>
      )}
      <output id="authNote" className="text-xs text-muted">
        {note ?? (who ? "" : "Sign in to chat.")}
      </output>
    </form>
  );
}
