---
name: diamond-review
description: Run a narrated Diamond review of a website/web app under development (voice + pointer + page telemetry), then summarize the findings in Persian. Use when the user wants to review, inspect or "check" pages of a site they are building.
---

# Diamond review

1. Make sure the project's dev server is running and you know its URL and repo path.
2. Start the review with the `diamond_start_review` MCP tool (or the CLI below) and tell the
   user, in Persian, to talk while pointing; `Alt+Shift+M` marks an element without speech;
   closing the window ends the session.
   ```bash
   diamond review <url> --repo <repo-path>
   ```
3. When the user says they are done, call `diamond_stop_review` (or wait for the CLI to finish).
4. Read the summary, then `diamond_findings(session, "SEV2")`.
5. Report back in Persian: counts by severity, the reviewer-reported items first, then the
   automatic ones, each with its likely source file. Put every path/selector in its own code
   block, never inline in a Persian sentence.
6. Offer to fix: hand each chosen finding to the `diamond-fix` skill. Do not edit code here.

Skipped analyzers are listed with their reason; never describe a skipped check as passed.
