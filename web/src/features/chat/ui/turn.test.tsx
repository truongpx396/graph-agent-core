// The parts of a turn a person acts on or waits for: the status line, the activity rail and the approval card.
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Approval, Step } from "../model/entry";
import { ActivityRail } from "./ActivityRail";
import { ApprovalCard } from "./ApprovalCard";
import { StatusLine, WAITING_AFTER_MS } from "./StatusLine";

const step = (over: Partial<Step> = {}): Step => ({
  callId: "c1", tool: "search_docs", label: "Search the knowledge base", tier: "read_only", subagent: null,
  outcome: "ok", durationMs: 1200, summary: "6 results", ...over,
});

describe("the status line", () => {
  afterEach(() => void vi.useRealTimers());

  it("says nobody has the turn yet after a while, and stops animating", () => {
    vi.useFakeTimers();
    const { container } = render(<StatusLine status="Working" started={false} since={Date.now()} />);
    expect(screen.getByRole("status").textContent).toBe("Working");
    act(() => void vi.advanceTimersByTime(WAITING_AFTER_MS));
    expect(screen.getByRole("status").textContent).toBe("Waiting to start");
    expect(container.querySelector(".shimmer")).toBeNull();
  });

  it("keeps saying what is happening once a worker has it", () => {
    vi.useFakeTimers();
    const { rerender, container } = render(<StatusLine status="Working" started={false} since={Date.now()} />);
    rerender(<StatusLine status="Working" started since={Date.now()} />);
    act(() => void vi.advanceTimersByTime(WAITING_AFTER_MS * 2));
    expect(screen.getByRole("status").textContent).toBe("Working");
    expect(container.querySelector(".shimmer")).not.toBeNull();
  });
});

describe("the activity rail", () => {
  it("animates only the running step, in the server's words", () => {
    const { container } = render(<ActivityRail steps={[step(), step({ callId: "c2", outcome: "running", durationMs: null, summary: null, label: "Calculate" })]} done={false} workedMs={0} />);
    expect([...container.querySelectorAll(".tool-activity-line")].map((row) => row.textContent)).toEqual(["Reads: Search the knowledge base6 results1 s", "Reads: Calculate"]);
    expect([...container.querySelectorAll(".shimmer")].map((node) => node.textContent)).toEqual(["Calculate"]);
  });

  it("does not call a very quick step zero seconds", () => {
    render(<ActivityRail steps={[step({ durationMs: 5 })]} done={false} workedMs={0} />);
    expect(screen.getByRole("listitem").textContent).toContain("<0.1 s");
  });

  it("gives handing work to a specialist a shape of its own, named for a screen reader", () => {
    const steps = [step({ tool: "run_subagent", label: "Ask a specialist" }), step({ callId: "c2" }), step({ callId: "c3", tool: "run_subagent", outcome: "refused" })];
    const { container } = render(<ActivityRail steps={steps} done={false} workedMs={0} />);
    expect([...container.querySelectorAll(".tool-activity-line svg")].map((icon) => icon.getAttribute("data-icon"))).toEqual(["robot", "read_only", "lock"]);
    expect(container.querySelector(".tool-activity-line")?.textContent).toContain("Specialist: Ask a specialist");
  });

  it("folds to one line when the turn is over, and opens again", async () => {
    const { container } = render(<ActivityRail steps={[step(), step({ callId: "c2" })]} done workedMs={8300} />);
    expect(container.querySelector(".tool-activity-line")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /Worked for 8 s · 2 steps/ }));
    expect(container.querySelectorAll(".tool-activity-line")).toHaveLength(2);
  });

  it("puts a specialist's calls under its name and shows a refusal with a lock, not as an error", () => {
    const { container } = render(
      <ActivityRail
        steps={[step({ label: "Ask a specialist" }), step({ callId: "n1", subagent: "researcher" }), step({ callId: "c3", outcome: "refused", summary: null })]}
        done={false}
        workedMs={0}
      />,
    );
    expect(screen.getByText("Asked the researcher subagent")).toBeTruthy();
    const refused = [...container.querySelectorAll(".tool-activity-line")].at(-1);
    expect(refused?.textContent).toContain("Not allowed");
    expect(refused?.querySelector('[data-icon="lock"]')).not.toBeNull();
    expect(refused?.textContent).toContain("Blocked: ");
    expect(refused?.className).not.toContain("danger");
  });
});

const approval: Approval = {
  id: "apr_1",
  calls: [{ name: "create_ticket", args: { subject: "VPN down" } }],
  items: [{ call_id: "c1", tier: "mutating", title: "Create a support ticket", effect: 'Opens ticket "VPN down"', target: "Support queue", visible_to: "Support team", reversible: false }],
};

function card() {
  const onDecide = vi.fn<(approved: boolean) => void>();
  return { onDecide, user: userEvent.setup(), ...render(<ApprovalCard approval={approval} onDecide={onDecide} />) };
}

describe("the approval card", () => {
  it("never takes the focus, so Enter in a hurry cannot approve", async () => {
    const { user, onDecide } = card();
    expect(document.activeElement).toBe(document.body);
    await user.keyboard("{Enter}{Escape}");
    expect(onDecide).not.toHaveBeenCalled();
  });

  it.each([["{Enter}", true], ["{Escape}", false]])("answers %s while the card has the focus", async (key, approved) => {
    const { user, onDecide, container } = card();
    container.querySelector<HTMLElement>(".approval")?.focus();
    await user.keyboard(key);
    expect(onDecide).toHaveBeenCalledExactlyOnceWith(approved);
  });

  it("answers once for Enter on a button", async () => {
    const { user, onDecide } = card();
    screen.getByRole("button", { name: "Decline" }).focus();
    await user.keyboard("{Enter}");
    expect(onDecide).toHaveBeenCalledExactlyOnceWith(false);
  });

  it("keeps the tool's name and arguments behind Technical details", () => {
    const { container } = card();
    const details = container.querySelector("details");
    expect(details?.open).toBe(false);
    expect(details?.textContent).toContain("create_ticket");
    details?.remove();
    expect(container.textContent).not.toContain("create_ticket");
    expect(container.textContent).toContain("Changes data");
  });
});
