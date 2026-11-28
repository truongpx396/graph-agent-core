import { useInfiniteQuery, useQuery } from "@tanstack/react-query";
import { useMemo } from "react";
import { type History, replayedEntry, useChat } from "@/features/chat";
import { useWorkspace } from "@/features/workspace";
import { listSessions, readTranscript } from "./api";

/** A conversation belongs to tenant, principal and domain, so all three are part of every cache key. */
function useScope() {
  const tenant = useWorkspace((state) => state.tenant);
  const principal = useWorkspace((state) => state.principal);
  const domain = useWorkspace((state) => state.domain);
  return [tenant, principal, domain] as const;
}

export const SESSIONS_KEY = "sessions";

export function useSessions(enabled: boolean) {
  const scope = useScope();
  return useQuery({ queryKey: [SESSIONS_KEY, ...scope], queryFn: listSessions, enabled });
}

export function useHistory(): History {
  const scope = useScope();
  const threadId = useChat((state) => state.historyThreadId);
  const query = useInfiniteQuery({
    queryKey: ["history", ...scope, threadId],
    queryFn: ({ pageParam }) => readTranscript(threadId ?? "", pageParam),
    initialPageParam: undefined as number | undefined,
    // `index` is a message's place in the whole transcript: 0 means there is nothing before this page.
    getNextPageParam: (page) => {
      const oldest = page[0]?.index;
      return oldest === undefined || oldest <= 0 ? undefined : oldest;
    },
    enabled: threadId !== null,
    // What is said after opening is kept by the chat store; reading it again would show it twice.
    staleTime: Number.POSITIVE_INFINITY,
    gcTime: 0,
  });
  const entries = useMemo(() => (query.data?.pages ?? []).toReversed().flat().map(replayedEntry), [query.data]);
  return {
    entries,
    loading: threadId !== null && query.isPending,
    hasEarlier: query.hasNextPage,
    loadingEarlier: query.isFetchingNextPage,
    // A failed "load earlier" keeps what is already shown and leaves the button to press again.
    failed: query.isError && entries.length === 0,
    loadEarlier: () => void query.fetchNextPage(),
    retry: () => void query.refetch(),
  };
}
