import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import { useAuth } from "@/features/auth";
import { useChat } from "@/features/chat";
import { useUploads } from "@/features/uploads/store";
import { useWorkspace } from "@/features/workspace";

// Zustand stores are module singletons: put each back so no test sees another's conversation or sign-in.
function restorer<S>(store: { getState: () => S; setState: (state: S, replace: true) => void }): () => void {
  const initial = store.getState();
  return () => store.setState(initial, true);
}
const restore = [restorer(useAuth), restorer(useChat), restorer(useUploads), restorer(useWorkspace)];

afterEach(() => {
  cleanup();
  for (const reset of restore) reset();
  localStorage.clear();
  sessionStorage.clear();
  document.head.innerHTML = "";
});
