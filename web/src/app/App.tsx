import { AuthBox, useAuth } from "@/features/auth";
import { Composer, Transcript, useChat } from "@/features/chat";
import { SessionList, useHistory } from "@/features/sessions";
import { ThemeToggle } from "@/features/theme";
import { UploadPanel } from "@/features/uploads";
import { DomainBrief, DomainSelect, IdentityPickers } from "@/features/workspace";

export function App() {
  const ready = useAuth((state) => state.ready);
  const identity = useAuth((state) => state.config.mode === "identity");
  const send = useChat((state) => state.send);
  const history = useHistory();
  return (
    <div className="flex h-screen flex-row max-[700px]:h-auto max-[700px]:min-h-screen max-[700px]:flex-col">
      {/* Where am I. Two halves that scroll on their own, so neither a long brief nor a long history pushes the other off screen. */}
      <aside id="domainSidebar" className="flex h-screen w-68 shrink-0 flex-col border-r border-line bg-sidebar max-[700px]:h-auto max-[700px]:w-auto max-[700px]:border-r-0 max-[700px]:border-b">
        <div className="flex min-h-0 flex-1 basis-0 flex-col gap-3.5 overflow-y-auto px-4 py-5 max-[700px]:max-h-[20vh] max-[700px]:flex-none">
          <DomainSelect />
          <DomainBrief onAsk={(question) => void send(question)} />
        </div>
        <SessionList ready={ready} className="min-h-0 flex-1 basis-0 border-t border-line max-[700px]:max-h-[20vh] max-[700px]:flex-none" />
      </aside>
      <main className="mx-auto flex h-screen w-full max-w-[760px] min-w-0 flex-1 flex-col px-5 py-4 max-[700px]:h-auto max-[700px]:min-h-[60vh]">
        {/* Who am I, top right: a different kind of scope from the two in the sidebar. */}
        <header className="mb-4 flex flex-wrap items-center gap-2 border-b border-line pb-4">
          <h1 className="flex flex-1 items-center gap-2 whitespace-nowrap text-[1.05rem] font-semibold tracking-tight">
            <span className="size-2.5 shrink-0 rounded-full bg-accent ring-3 ring-accent-soft" />
            Graph Agent Core
          </h1>
          {identity ? <AuthBox /> : <IdentityPickers />}
          <ThemeToggle />
        </header>
        <UploadPanel />
        <Transcript history={history} />
        <Composer />
      </main>
    </div>
  );
}
