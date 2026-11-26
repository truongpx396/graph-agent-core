// The composition root: the only place that joins one feature to another. Features do not import each other (but for the two edges
// src/test/architecture.test.ts allows), so what they need from one another is bound here.
import type { QueryClient } from "@tanstack/react-query";
import { authorize, useAuth } from "@/features/auth";
import { useChat } from "@/features/chat";
import { SESSIONS_KEY } from "@/features/sessions";
import { useWorkspace, workspaceHeaders } from "@/features/workspace";
import { bindRequestContext } from "@/shared/api";

export function wire(queryClient: QueryClient): () => void {
  bindRequestContext({
    headers: () => authorize(workspaceHeaders()),
    onUnauthorized: () => useAuth.getState().unauthorized(),
  });

  // A conversation belongs to the tenant, principal and domain that started it, and the server answers anyone else's send, resume or
  // cancel on it with a 404 (a domain also runs a different graph). So changing any of them starts a new one.
  const offWorkspace = useWorkspace.subscribe((next, previous) => {
    if (next.tenant !== previous.tenant || next.principal !== previous.principal || next.domain !== previous.domain) {
      useChat.getState().startFresh();
    }
  });

  // A turn that ends or pauses has just written its conversation's row, so the list is stale. A pause counts: a first turn that
  // pauses never ends on its own.
  const offChat = useChat.subscribe((next, previous) => {
    const settled = (previous.turn !== null && next.turn === null) || (next.turn?.phase === "paused" && previous.turn?.phase === "streaming");
    if (settled) void queryClient.invalidateQueries({ queryKey: [SESSIONS_KEY] });
  });

  return () => {
    offWorkspace();
    offChat();
  };
}
