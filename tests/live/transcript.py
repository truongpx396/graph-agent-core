"""What the web UI showed, as text, for the report of a failed e2e test.

An e2e failure used to say only what one locator held ("expected text 'Dana Whitfield'; actual: ..."), which is
not enough to tell a model that answered wrongly from one that called the wrong tool, paused for approval, or
never started. The page renders all of that: the user's message, a row per tool call (the server's label, result
and duration), a notice, the approval card, the status line and the answer.

A tool's identifier and arguments are on the page only for a call that waits for approval (web/DESIGN.md), so
for any other call this has the label and result, not the arguments.

Best effort by design: whatever calls this must never let it replace the failure it is describing.
"""
from typing import Any

# The class names are web/src/features/chat/ui/'s. A finished turn folds its tool rows behind `.activity-summary`, so
# they are opened first; the approval card sits below `#messages`, not inside the message that waits for it.
_TRANSCRIPT_JS = """async () => {
  const folded = document.querySelectorAll('#messages .activity-summary[aria-expanded="false"]');
  folded.forEach((button) => button.click());
  if (folded.length) await new Promise((resolve) => setTimeout(resolve, 50));

  const text = (node) => (node ? node.textContent.trim() : '');
  // For a row made of several elements, which textContent would run together.
  const words = (node) => [...node.childNodes]
    .map((child) => (child.firstElementChild ? words(child) : child.textContent.trim()))
    .filter(Boolean).join(' ');

  const lines = [];
  for (const message of document.querySelectorAll('#messages .msg')) {
    if (!message.classList.contains('assistant')) {
      lines.push((message.classList.contains('user') ? 'user: ' : 'system: ') + text(message));
      continue;
    }
    const answer = [...message.querySelectorAll('.answer-text')].map(text).filter(Boolean).join(' ');
    const status = text(message.querySelector('.status'));
    lines.push('assistant: ' + (answer || '(no answer text)') + (status ? ' [status: ' + status + ']' : ''));
    for (const row of message.querySelectorAll('.activity-summary, .tool-activity-line')) lines.push('  ' + words(row));
    for (const declined of message.querySelectorAll('.declined')) lines.push('  ' + text(declined));
    for (const notice of message.querySelectorAll('.notice')) lines.push('  notice (' + notice.dataset.tone + '): ' + text(notice));
  }
  for (const card of document.querySelectorAll('.approval')) {
    lines.push('approval card: ' + text(card.querySelector('.approval-question')));
    for (const item of card.querySelectorAll('.approval-item')) lines.push('  ' + words(item));
    for (const call of card.querySelectorAll('.approval-details pre')) lines.push('  ' + text(call).replace(/\\s+/g, ' '));
  }
  return lines.join('\\n');
}"""


def page_transcript(page: Any) -> str:
    """The conversation on the page, one message per block, then the approval card if one is waiting."""
    return page.evaluate(_TRANSCRIPT_JS) or "(no messages on the page)"
