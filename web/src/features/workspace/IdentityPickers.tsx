import { control, cx, label } from "@/shared/lib/classes";
import { type IdentityField, useWorkspace } from "./store";

const ADD_NEW = "__add_new__";

function Picker({ field, title, caption }: { field: IdentityField; title: string; caption: string }) {
  const value = useWorkspace((state) => state[field]);
  const values = useWorkspace((state) => state.recent[field]);
  const setIdentity = useWorkspace((state) => state.setIdentity);
  const pick = (picked: string) => {
    // "+ Add new…" makes typing an unseen value discoverable. Cancelled or blank leaves the selection as it was.
    const next = picked === ADD_NEW ? (window.prompt(`New ${field}:`) ?? "").trim() : picked;
    if (next) setIdentity(field, next);
  };
  return (
    <div className="flex shrink-0 flex-col gap-0.5">
      <label htmlFor={field} className={label}>
        {caption}
      </label>
      <select id={field} title={title} className={cx(control, "max-w-36")} value={value} onChange={(event) => pick(event.target.value)}>
        {values.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
        <option value={ADD_NEW}>+ Add new…</option>
      </select>
    </div>
  );
}

export function IdentityPickers() {
  return (
    <>
      <Picker field="tenant" caption="Tenant" title="Trusted-layer tenant id (X-Tenant-Id). A real deployment sets this from its sign-in, never from a field." />
      <Picker field="principal" caption="Principal" title="Your principal id (X-Principal-Id): which sessions and memories are yours within the tenant." />
    </>
  );
}
