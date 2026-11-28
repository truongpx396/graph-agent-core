# Web client design rules

For anyone, person or agent, changing the UI. `src/test/design.test.ts` holds the first two rules of each section it can; the rest are for review.

**The server states facts, the page renders them.** A tool's label, tier and effect, whether it can be undone, an error's kind and a turn's usage all
arrive on the stream (`openapi.json`). The page never reads a tool's arguments to guess a name or a risk, and renders only documented events: one it
does not know is ignored, and `shared/api/contract.test.ts` fails when the contract gains one.

## Colour

- Only the tokens in `app/styles.css` (`bg-elevated`, `text-muted`, `border-line`, `text-danger`, ...). No hex, no `rgb()`, no Tailwind palette name.
- Colour never carries meaning alone: a state also has words or an icon.
- `danger` is for something that failed. A refusal is not a failure.

## Status words

| State | Words | Looks |
|---|---|---|
| queued | `Working` | muted, shimmer |
| working | `Working`, `Rewriting the answer…`, `Summarizing older messages` | muted, shimmer |
| waiting for a worker (10 s) | `Waiting to start` | muted, still |
| waiting for the person | `Waiting for your approval` and the card above the composer | muted, still |
| blocked | what the rule is, then who can change it | neutral ink, lock |
| failed | what happened, then what to do | danger |
| lost | what is gone, then what to do | neutral ink |
| done | steps fold to `Worked for 8 s · 3 steps` | muted |

## Tool calls and approvals

- Never put a tool's identifier or raw JSON on screen. The one exception is the card's **Technical details**, closed by default.
- A call without a server label is "A step"; a pause without items is "Run an action". Say less, never the identifier.
- A step the agent recovers from stays neutral.
- An approval is only ever the person's action. The card does not take the focus, and its keys (⏎ approve, esc decline) work only while it has it:
  a global Enter would approve a write for someone typing. Every answer names the pause it was shown (`approval_id`).
- Say an answer took effect only once the stream goes on. A refused or failed answer reads the pause again and shows the card.
- Show no control the server cannot honour: no "edit", no "always allow".

## Numbers

- Unknown is not zero: `tokens unknown`, `cost unknown`.
- Machine values (model, tokens, cost, durations) are small, muted and monospaced.

## Copy

- Sentence case. Buttons start with a verb: Approve, Decline, Retry, Upload.
- One word per thing: **conversation** (not session, not chat).
- No descriptive subtitles; if an explanation is needed once, it is a `title`.
- An error says what happened and what to do. Words written for an operator are replaced (`features/chat/model/notice.ts`).
- No developer notes, pattern numbers or file names.

## States

Every area that reads data has four: loading (a skeleton, not "Loading…"), empty (what to do next), failed (`Couldn't load … · Retry`) and, for a
live turn, disconnected (`Reconnecting… picks up where it left off`).
Signed out is not a failure: say to sign in, read nothing, and offer no Retry.

## Motion

Only what is in progress moves. `prefers-reduced-motion` stops all of it.
