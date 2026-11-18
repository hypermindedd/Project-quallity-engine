# Diamond engine — instructions for any AI assistant

> فارسی: وقتی کاربر گفت «موتور دایموند رو از توی درایو فراخونی کن و روی همین پروژه اجرا کن»، دقیقاً مراحل زیر را انجام بده.

Trigger phrases (Persian or English), e.g.:
«موتور دایموند رو از درایو فراخونی کن برای همین پروژه» · «دایموند رو روی این پروژه اجرا کن» · "run Diamond on this project".

Diamond is a narrated design & code review engine. The user talks while pointing at the running
site; Diamond records voice + pointer + page telemetry, finds issues, traces them to file:line and
writes a Persian report plus one fix brief per finding. Everything runs **on the user's computer**
(macOS by default). The engine lives in the user's Google Drive folder **`Diamondengine`**.

## 1. Requirements you must have
- A shell on the user's computer (Claude Code, Claude desktop with a device shell, Cursor, a terminal).
  Without one, tell the user and give them the commands from step 3 to paste.
- The project directory = the folder of "this project" (ask only if it is genuinely unclear).

## 2. Get the launcher (`diamond-run`)
Try in order, stop at the first that works:
1. Already installed: `command -v diamond-run`
2. Google Drive for desktop sync: `~/Library/CloudStorage/GoogleDrive-*/My Drive/Diamondengine/diamond-run`
3. Inside the newest `Diamondengine-src-*.zip` in that Drive folder: `diamond/bin/diamond-run`
4. If Drive is not synced to the computer: download the newest `Diamondengine-src-*.zip` and
   `diamond-run` from the Drive folder `Diamondengine` into `~/Downloads/Diamondengine/` on the
   user's computer (the launcher also looks there).

Optional, once: `bash diamond-run --install-self` (puts it on PATH as `diamond-run`).

## 3. Run it
```bash
cd <project-dir>
bash <path-to>/diamond-run --doctor        # shows engine source, Python, how the project will be served
bash <path-to>/diamond-run                 # narrated review (the user talks and points)
```
- First run installs the engine into `~/.diamond-engine` (own venv; needs Python 3.11+; if missing
  the launcher prints the exact `brew` command). Later runs reuse it and update automatically when
  a newer zip appears in Drive.
- It finds a running dev server or starts one (Next, Vite, Nuxt, Astro, SvelteKit, CRA, Angular,
  Gatsby, Remix, FastAPI, Flask, Django, plain HTML), and stops it afterwards. Python apps use the
  project's .venv (created and filled from requirements.txt on first run).
- If Google Chrome is installed it is used directly (no browser download). Use `--url http://localhost:PORT` if detection fails.
- Tell the user, in Persian: talk while pointing; `Alt+Shift+M` marks an element without speaking;
  close the browser window to finish.
- No human available / quick automatic check: `--smoke 20` (headless, no mic, 20 seconds).
- Re-analyse the last session without opening a browser: `--analyze-last`.

Exit codes: 3 missing prerequisite (message says what to install) · 4 engine not found · 5 dev server failed.

## 4. After the run
Newest session: `<project>/.diamond/sessions/<id>/`
- `report.md` — Persian report (the launcher opens it)
- `findings.json` — all findings with evidence and source candidates
- `briefs/<id>.md` — one fix brief per finding (model tier + acceptance rule)
- `pqe-findings.json`, `test_diamond_regressions.py` — for the PQE test engine

Reply to the user in Persian: counts by severity, reviewer-reported items first, then automatic
ones, each with its likely source file. Put paths/commands in their own code blocks, never inline in
Persian sentences. Then offer to fix.

## 5. Fixing (only when the user asks)
For each finding, follow its brief: confirm the root cause in code first (candidates below 0.8
confidence are guesses), fix at the root (tokens / shared component, no `!important`, no page
patches), never edit tests, work on a branch, and prove the fix by re-running
`diamond-run --smoke 20` or the generated regression test.

## 6. Model use (token budget)
Light model for summarising/classifying, standard for visual critique and single-file fixes,
heavy only for multi-file root causes and architecture. Deterministic analyzers cost no tokens.
