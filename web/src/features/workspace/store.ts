import { create } from "zustand";
import { localStore } from "@/shared/lib/storage";

// Who the caller says they are and which agent they are talking to. Tenant and principal are a demo convenience standing in for a real
// sign-in (app/adapters/inbound/api/deps.py): in identity mode they are never sent.

export const IDENTITY = {
  tenant: { key: "graph-agent-core:tenants", initial: "ecorp" },
  principal: { key: "graph-agent-core:principals", initial: "web-user" },
} as const;
export type IdentityField = keyof typeof IDENTITY;
const RECENT_MAX = 8;

function recent(field: IdentityField): string[] {
  try {
    const values: unknown = JSON.parse(localStore.get(IDENTITY[field].key) ?? "[]");
    return Array.isArray(values) ? values.filter((value) => typeof value === "string") : [];
  } catch {
    return [];
  }
}

function remember(field: IdentityField, value: string): string[] {
  const values = [value, ...recent(field).filter((other) => other !== value)].slice(0, RECENT_MAX);
  localStore.set(IDENTITY[field].key, JSON.stringify(values));
  return values;
}

export interface Workspace {
  tenant: string;
  principal: string;
  domain: string;
  recent: Record<IdentityField, string[]>;
  setIdentity: (field: IdentityField, value: string) => void;
  setDomain: (domain: string) => void;
}

export const useWorkspace = create<Workspace>((set) => ({
  tenant: IDENTITY.tenant.initial,
  principal: IDENTITY.principal.initial,
  domain: "ecorp",
  recent: {
    tenant: remember("tenant", IDENTITY.tenant.initial),
    principal: remember("principal", IDENTITY.principal.initial),
  },
  setIdentity(field, value) {
    const values = remember(field, value);
    set((state) => ({ [field]: value, recent: { ...state.recent, [field]: values } }));
  },
  setDomain: (domain) => set({ domain }),
}));

export function workspaceHeaders(): Record<string, string> {
  const { tenant, principal, domain } = useWorkspace.getState();
  // X-Domain is read only by the turn endpoints and the session list; the others ignore it.
  return { "X-Tenant-Id": tenant, "X-Principal-Id": principal, "X-Domain": domain };
}
