import { control, label } from "@/shared/lib/classes";
import { DOMAINS } from "./domains";
import { useWorkspace } from "./store";

export function DomainSelect() {
  const domain = useWorkspace((state) => state.domain);
  const setDomain = useWorkspace((state) => state.setDomain);
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor="domainSelect" className={label}>
        Domain
      </label>
      <select
        id="domainSelect"
        className={control}
        title="Which domain this turn runs against (X-Domain). Needs an agent-worker serving that domain (AGENT_WORKER_DOMAINS)."
        value={domain}
        onChange={(event) => setDomain(event.target.value)}
      >
        {DOMAINS.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </select>
    </div>
  );
}
