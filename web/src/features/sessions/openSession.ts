import { readPendingApproval, useChat } from "@/features/chat";

export async function openSession(threadId: string): Promise<void> {
  useChat.getState().open(threadId);
  // A turn paused for approval is not in the transcript: without this a reload makes it look resolved. Best effort, because a new
  // message on such a thread is refused with a clear error rather than corrupting it.
  const pending = await readPendingApproval(threadId).catch(() => null);
  if (pending) useChat.getState().restorePendingApproval(threadId, pending);
}
