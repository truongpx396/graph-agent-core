import { QueryClient } from "@tanstack/react-query";

// No automatic retries: a retried upload would index a document twice, and a refused read should say so, not stall.
export const createQueryClient = () =>
  new QueryClient({ defaultOptions: { queries: { retry: false, refetchOnWindowFocus: false }, mutations: { retry: false } } });
