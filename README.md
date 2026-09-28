# Repo Overwatch

An AI watchdog for GitHub repositories. It runs **only when you ask**: comment `run overwatch` on an issue or pull request and it scans the **whole repository**, then turns what it finds into a Markdown report, one GitHub issue per problem, and a pull request with validated fixes.

## How it works

```
                ┌──────────────────────────────────────────────┐
 "run overwatch" │ 1. Collect files, build the linked-file graph│
  comment ─────▶ │ 2. Semgrep · Gitleaks · OSV-Scanner          │  free, deterministic
                │ 3. Gemini: first pass over code + docs       │  broad and cheap
                │ 4. Verify each candidate, write fixes        │  strict second pass*
                │ 5. Report · issues · fix PR (validated)      │
                └──────────────────────────────────────────────┘
```

\* Verification is done by a second, deliberately skeptical Gemini pass, so **a free Gemini API key is all you need**. If you ever add an Anthropic API key, Claude takes over verification automatically, which gives an independent second opinion and catches more false positives.

| What it checks | How |
|---|---|
| Bugs, broken code, error handling, concurrency, type safety | Gemini first pass → verification |
| Unused and dead code, duplication, performance | Gemini first pass → verification |
| Security anti-patterns | Semgrep rules + AI review → verification |
| Committed secrets | Gitleaks (values are redacted and **never** sent to an AI model) |
| Vulnerable dependencies | OSV-Scanner on your lockfiles/manifests |
| Documentation that no longer matches the code | Gemini docs pass (docs vs. code outline + manifests) → verification |
| Broken links in Markdown | Deterministic link checker |
| Build, CI and config problems | Gemini first pass → verification |

Every finding lists **what's wrong, why it matters, the suggested change, the exact location, and the linked files that also need updating** (callers, implementations, tests, docs), found through an import graph that understands JavaScript/TypeScript (including Svelte, Vue, `$lib` and `@/` aliases), Python, Rust, Go, C/C++, Java/Kotlin, Ruby, PHP, Dart, CSS, HTML and Markdown links.

### Outputs

| Output | Where |
|---|---|
| Full Markdown report | The workflow run's **Summary** page, plus the `overwatch-report-…` artifact (report + JSON) |
| Pinned report | An issue labelled `overwatch-report`, refreshed on every default-branch run |
| One issue per finding | Labelled `overwatch` and `severity:*`, deduplicated across runs |
| Fix pull request | Branch `overwatch/fixes-<branch>`, with an inline review comment explaining each fix |
| Reply to your command | A comment in the same issue or pull request, mentioning you, with the top findings and links |

---

## Setup guide

You do this once. Parts 1–3 create the action; Part 4 installs it into any repository (repeat Part 4 for each repository you want watched).

### Part 1 — Get your Gemini API key (free)

1. Go to **https://aistudio.google.com** and sign in with a Google account.
2. Click **Get API key** → **Create API key**. Choose or create a Google Cloud project when asked.
3. Copy the key (it starts with `AIza`). Keep it somewhere safe for Part 4.
4. No billing is needed. Read [Running on the free tier](#running-on-the-free-tier) before your first scan, especially the privacy note.

**Optional, later: Anthropic (Claude).** If you ever get an Anthropic API key (console.anthropic.com), add it as a secret named `ANTHROPIC_API_KEY` and uncomment the `anthropic-api-key` line in the workflow. Claude then does the verification pass. Nothing else changes.

### Part 2 — Create the action repository

1. On GitHub, click **+** → **New repository**.
   - Name: `repo-overwatch`
   - Visibility: **Public** is simplest (any repository can use it). Private also works, see step 6.
   - Do **not** add a README, .gitignore or license (this project has them).
2. Unzip the downloaded `repo-overwatch.zip` on your computer and open a terminal in the unzipped `repo-overwatch` folder.
3. Put your GitHub username into two places:
   - `action.yml`: change `author: "your-username"`.
   - `templates/overwatch.yml`: change `uses: YOUR-GITHUB-USERNAME/repo-overwatch@v1`. (also in `templates/overwatch-auto.yml` if you plan to use automatic mode).
4. (Optional but recommended) run the tests:

   ```bash
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt pytest
   PYTHONPATH=src pytest -q          # expect: 21 passed
   ```

5. Push it:

   ```bash
   git init -b main
   git add .
   git commit -m "Repo Overwatch v1.0.0"
   git remote add origin https://github.com/your-real-username/repo-overwatch.git
   git push -u origin main
   ```

6. **Only if the repository is private:** open **Settings → Actions → General**, scroll to **Access**, and choose **Accessible from repositories owned by the user 'your-real-username'**, then **Save**. This lets your other **private** repositories use it. To scan **public** repositories with a private tool, follow Part 4b instead.

### Part 3 — Publish a version

Workflows refer to the action by tag (`@v1`). Create a full version tag and a moving major tag:

```bash
git tag -a v1.0.0 -m "v1.0.0"
git tag -f v1
git push origin v1.0.0
git push origin v1 --force
```

Whenever you change the action later: commit, tag the new version (`v1.0.1`), move `v1` again with the same two last commands, and every repository using `@v1` picks it up on its next run.

(Optional) To list it on the GitHub Marketplace, open the repository's **Releases** → **Draft a new release** → tick **Publish this Action to the GitHub Marketplace**. The action name must be unique on the Marketplace; rename it in `action.yml` if needed.

### Part 4 — Install it in a repository

Do these steps in the repository you want watched (for example your app's repository).

**4.1 Add the API keys as secrets**

1. Open the repository → **Settings → Secrets and variables → Actions**.
2. Click **New repository secret**: name `GEMINI_API_KEY`, value = your Gemini key → **Add secret**.
3. That's the only secret you need. (Optional: an `ANTHROPIC_API_KEY` secret if you later want Claude to verify.)

Tip: if you will install Overwatch in many repositories of an organization, add these once as **organization secrets** instead.

**4.2 Allow the workflow to write**

1. Open **Settings → Actions → General** and scroll to **Workflow permissions**.
2. Select **Read and write permissions**.
3. Tick **Allow GitHub Actions to create and approve pull requests** (required for the fix PR).
4. Click **Save**.

If these options are greyed out, an organization policy controls them: change the same settings under the organization's **Settings → Actions → General** first.

**4.3 Add the workflow file**

You now have two repositories. `repo-overwatch` holds the tool itself (Parts 2–3). The repository you want scanned (your app, for example) only needs **one small file** that tells GitHub to use the tool. This step adds that file to the repository you want scanned, **not** to `repo-overwatch`.

Easiest way, on the GitHub website:

1. Open `repo-overwatch` on GitHub, open `templates/overwatch.yml`, and click the **Copy raw file** button (the two-squares icon above the file). Copying from GitHub avoids text editors that turn `"` into curly quotes, which breaks YAML.
2. Open the repository you want scanned. Check that the branch selector (top left) shows the default branch, usually `main`.
3. Click **Add file → Create new file**.
4. In the file name box, type `.github/workflows/overwatch.yml`. Each `/` you type turns the text before it into a folder; that is expected.
5. Paste into the large editor box.
6. Near the bottom, find `- uses: YOUR-GITHUB-USERNAME/repo-overwatch@v1` and make sure it has your GitHub username (it already does if you edited it in Part 2). Change nothing else.
7. Click **Commit changes…**, keep **Commit directly to the `main` branch**, and click **Commit changes**.

Done. The file must be on the default branch because GitHub only runs comment-triggered workflows from there.

Prefer the terminal? The same thing from your computer:

```bash
cd /path/to/your-project
mkdir -p .github/workflows
cp /path/to/repo-overwatch/templates/overwatch.yml .github/workflows/overwatch.yml
# edit the "uses:" line if needed, then:
git add .github/workflows/overwatch.yml
git commit -m "Add Repo Overwatch"
git push origin main
```

This workflow runs only on command (see Part 5). If you would rather scan automatically on every push, pull request and new branch, use `templates/overwatch-auto.yml` instead; it costs more because every change triggers a scan. Use one of the two files, not both.

**4.4 (Optional) Add a config file: skip this for your first scan**

Overwatch works without a config file. Come back to this once you have seen a first report and want to tune it.

The most useful setting is `validate:`: commands that must pass, with the fixes applied, before Overwatch opens a fix pull request (for example, "the project still builds and its tests pass"). To add it:

1. In the scanned repository, create a file named `.overwatch.yml` in the **root** (same **Add file → Create new file** steps as above), paste the contents of `templates/.overwatch.yml`, and edit the `validate:` list, for example for a Node project:

   ```yaml
   validate:
     - npm ci
     - npm test
   ```

2. Those commands run on GitHub's machine, which only has the tools you install. So in `.github/workflows/overwatch.yml`, add a setup step **between** the checkout step and the Overwatch step (the template has this example commented out):

   ```yaml
         - uses: actions/checkout@v6
           with:
             ref: ${{ steps.target.outputs.branch }}

         - uses: actions/setup-node@v6        # added: installs Node so npm commands can run
           with:
             node-version: 22

         - uses: your-real-username/repo-overwatch@v1
   ```

   For Rust (`cargo test`), add `- uses: dtolnay/rust-toolchain@stable` the same way.

Without `validate:`, fix PRs are still opened but clearly marked as not built or tested.

**4.5 That's it: no scan starts yet**

If you used the website, your file is already committed; if you used the terminal, the commands above pushed it. Adding the workflow does not start a scan by itself. That's the point: nothing runs, and nothing is spent, until you give a command in Part 5.

### Part 4b — Keep repo-overwatch private while scanning a public repository (optional)

GitHub lets a **private** action be used only by other **private** repositories of the same owner. To keep `repo-overwatch` private and still scan a **public** repository, the workflow downloads the tool with a read-only token and runs it as a local action. Your tool's source code is never published.

**1. Create a read-only token for the tool**

1. On GitHub, click your avatar → **Settings** → **Developer settings** (bottom of the left sidebar) → **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
2. **Token name:** `overwatch-action-read`. **Expiration:** pick a date (for example 1 year) and note it; see step 4.
3. **Resource owner:** your account. **Repository access:** **Only select repositories** → choose `repo-overwatch` only.
4. **Permissions → Repository permissions → Contents:** **Read-only**. (GitHub adds *Metadata: Read-only* automatically.) Leave everything else as *No access*.
5. Click **Generate token** and copy it (it starts with `github_pat_`). You will not see it again.

**2. Store it in the public repository you want scanned**

**Settings → Secrets and variables → Actions → New repository secret**: name `OVERWATCH_ACTION_TOKEN`, value = the token. (Your `GEMINI_API_KEY` secret stays as it is.)

**3. Use the private-tool workflow**

Use `templates/overwatch-private-tool.yml` instead of `templates/overwatch.yml` as the content of `.github/workflows/overwatch.yml` (same steps as 4.3). Replace `YOUR-GITHUB-USERNAME` in the `repository:` line with your username. Commit it to the default branch. Everything else, including the `run overwatch` commands, works exactly the same.

The **Access** setting in `repo-overwatch` is not used by this method; you can leave it or set it to *Not accessible*.

**4. Good to know**

- The token can only *read* `repo-overwatch`. Secrets are hidden in logs and are not available to pull requests from forks, and Overwatch refuses to run on fork pull requests anyway.
- When the token expires, scans fail at the step *Download the private Repo Overwatch tool*. Generate a new token the same way and update the `OVERWATCH_ACTION_TOKEN` secret.
- What *is* public is what always is for a public repository: the run logs, the report on the run page, the report artifact, and the issues and pull requests Overwatch creates. These describe your public code, not the tool.
- The downloaded tool lives in a `.overwatch-action` folder during the run. Overwatch never scans it and never includes it in fixes.

### Part 5 — Run your first scan

1. Open the repository's **Issues** tab and create an issue, for example titled `Overwatch commands`. You can reuse this issue for every future command.
2. Add a comment that says exactly:

   ```
   run overwatch
   ```

3. Within a few seconds a 👀 reaction appears on your comment: the command was received.
4. Open the **Actions** tab and click the **Repo Overwatch** run to follow progress. The first scan takes longest: it installs the scanners (cached afterwards) and sends every file to the models. Later scans re-analyze only files that changed since the last scan; the report still covers the whole repository.
5. When it finishes, Overwatch replies to your comment, mentioning you, with the top findings and links. You also get:
   - the full report on the run's **Summary** page, and in the **Artifacts** section (`overwatch-report-…`, Markdown + JSON)
   - one issue per finding, plus the pinned `Repo Overwatch report` issue (default branch only)
   - a pull request `Repo Overwatch: N automated fixes for main`, with a review comment on each change

If a scan fails, Overwatch comments on your issue with a link to the failed run.

### Part 6 — Email notifications

Overwatch does not send email itself; GitHub emails you about the issues and pull requests it opens.

1. Open **github.com → your avatar → Settings → Notifications**.
2. Under **Default notifications email**, check the address.
3. Under **Subscriptions → Watching**, make sure **Email** is ticked.
4. In the watched repository, click **Watch** (top right) and choose **All Activity**, or **Custom** with **Issues** and **Pull requests** ticked.

---

## Everyday use

### Commands

| To scan… | Do this |
|---|---|
| The default branch | Comment `run overwatch` on any issue |
| Another branch | Comment `run overwatch on feature/login` on any issue |
| A pull request's branch | Comment `run overwatch` on the pull request (the fix PR will target that branch) |
| The branch you're pushing | Put `[run overwatch]` anywhere in the commit message, for example `git commit -m "Refactor upload [run overwatch]"` |
| Any branch, from the web | **Actions → Repo Overwatch → Run workflow**, optionally typing a branch name |

Commands are not case-sensitive, but the comment must **start** with `run overwatch`. Only the repository owner, organization members and collaborators can trigger a scan, so on a public repository strangers can't spend your API credits. Scans on pull requests from forks are refused for safety.

To use a different phrase, edit the two places in `.github/workflows/overwatch.yml` that contain `run overwatch` (the `startsWith(...)` check and the `sed` line in the "Work out which branch to scan" step).

**Triage a finding**

- Real problem: fix it (or merge the fix PR). On the next default-branch scan Overwatch closes the issue automatically.
- False positive or won't fix: close the issue as **Close as not planned**, or add the label **`overwatch-ignore`**. Overwatch will not reopen or update it.
- A problem that comes back after being fixed is reopened automatically.

**Only see what matters.** Set `min_severity: medium` in `.overwatch.yml`.

**Flag serious problems loudly.** Set `fail_on: high` to mark the scan as failed (red ✗) when a high or critical finding exists. Making it a required merge check only works with the automatic workflow (`overwatch-auto.yml`), because on-command scans don't run on every pull request.

**Try it locally without touching GitHub** (dry run, writes the report and a `.patch` file to your temp folder):

```bash
cd repo-overwatch
pip install -r requirements.txt
export GEMINI_API_KEY=AIza...
PYTHONPATH=src python -m overwatch --workspace /path/to/your/project --dry-run
```

Semgrep, Gitleaks and OSV-Scanner are used if installed locally (`bash scripts/install-tools.sh ~/.overwatch-tools` and add `~/.overwatch-tools/bin` to `PATH`); otherwise they are skipped.

## Running on the free tier

Overwatch's defaults are tuned for a free Gemini key:

- **Model:** `gemini-3.5-flash-lite`. On the free tier, Flash-Lite models currently get a much larger daily request quota than the regular Flash models (hundreds of requests per day versus a few dozen), and a full scan needs several requests.
- **Pacing:** at most 10 requests and about 200,000 tokens per minute per model, so Overwatch waits instead of hitting per-minute limits. If Google still answers "too many requests", Overwatch waits the time Google asks for and retries.
- **Per-scan cap:** at most 100 AI requests per scan (`max_ai_requests`).
- **Fewer requests:** candidates from several files are verified together in one request, and unchanged files are never re-sent.

**How many requests does a scan use?** Roughly one request per 250 KB of code for the first pass, one or two for the documentation check, and one per batch of up to 40 candidates for verification. A small or medium repository usually needs 5–20 requests for its first scan, and far fewer afterwards because only changed files are re-analyzed. The report's *Run details* section shows the exact count.

**Check your real limits.** Google sets free quotas per project and model and changes them from time to time. Google AI Studio (**https://aistudio.google.com**) shows your project's current rate limits and usage on its usage/rate-limit page. Set `gemini_rpm` in `.overwatch.yml` a little below your requests-per-minute limit.

**When the quota runs out.** Overwatch stops cleanly instead of failing. The report and its reply to your command say *"This scan stopped early to stay within your AI quota"*, and everything finished so far is kept. Comment `run overwatch` again to continue from where it stopped. If the *daily* quota was used up, wait until it resets at midnight Pacific time. Issues are never auto-closed after an incomplete scan.

**Getting more out of the quota:**
- Narrow the scan with `include:` (for example only `src/**`) or add `exclude:` patterns for generated code.
- Set `min_severity: medium` to skip filing low-severity cleanups (this reduces noise, not requests).
- For a stronger verification pass, set `gemini_verify_model: gemini-3.5-flash`, but only if your quota for that model allows it. Flash's free daily quota is much smaller, and a scan will stop early when it runs out.
- A model that is not available on the free tier makes the scan stop immediately with a quota message. Switch back to a Flash-Lite model.

**⚠️ Privacy on the free tier.** Under Google's Gemini API terms, content sent to the *unpaid* tier may be used by Google to improve its products, and human reviewers may read it. Google's terms tell you not to submit sensitive or confidential information. In practice:
- **Open-source or public repositories:** fine, since the code is public anyway.
- **Private or client code:** don't scan it with a free key. Use `include:`/`exclude:` to keep sensitive parts out, or enable billing on the Google Cloud project; paid usage is not used to improve Google's products. With billing enabled, the same key keeps working, so you can then raise `gemini_rpm` and `max_ai_requests`.

Secret values found by Gitleaks are never sent to any AI model, on any tier.

## Behaviour and safeguards

- **Only on command:** every other push, comment and pull request event starts a workflow job that is skipped instantly, which uses no runner minutes and makes no AI calls. Repeated commands queue up rather than cancelling a scan that is already running (and already paid for).
- **Branches:** each scanned branch gets its own fix PR (`overwatch/fixes-<branch>`), targeting that branch. Issues are shared across branches and auto-closed only from the default branch, and only when the scan completed without errors.
- **Pull requests:** commenting `run overwatch` on a PR scans its branch and the fix PR targets that branch. Pull requests from forks are refused: a comment-triggered workflow has access to your secrets, and fork code must never run with them.
- **Skips itself:** Overwatch does not review the workflow file that runs it (whatever you named it) or its `.overwatch.yml` config. Your other workflow files are still checked, and the secret scan still covers every file.
- **No loops:** Overwatch never scans its own `overwatch/*` branches, and its own comments never contain the command.
- **Rate of new issues:** at most `max_new_issues` (default 20) new issues per run. The rest are listed in the report and filed on later runs.
- **Deduplication:** each issue carries a fingerprint of the file path and the flagged code (whitespace-insensitive), so line shifts don't create duplicates. If the flagged code itself changes, the old issue is closed and a new one opened.
- **Fixes are conservative:** a patch is applied only if its text matches the file exactly once; a finding's edits are all-or-nothing; workflow files are never edited; if `validate:` commands fail, no PR is opened and the output is in the report.
- **Re-runs are quiet:** if the fixes are identical to what is already on the fix branch, nothing is pushed and no new review comments are posted.
- **Cost control:** results are cached by file content hash, so unchanged files are not re-sent to the models.

## Privacy and security

- File contents (excluding dependencies, build output, binaries, lockfiles and secret-like files such as `.env` and `*.pem`) are sent to the Google Gemini API (and the Anthropic API if you add a key). On the Gemini free tier this content may be used by Google and read by reviewers; see [Running on the free tier](#running-on-the-free-tier). Use `exclude:` to keep specific paths out.
- Secrets detected by Gitleaks are redacted and never sent to an AI model. Secret findings become issues; in a public repository those issues are public, but the secret was already public in the code, so rotate it immediately.
- Pull requests opened with the default `GITHUB_TOKEN` **do not trigger other workflows** (a GitHub safeguard), so your CI will not run on the fix PR automatically. To change that, create a fine-grained personal access token (or GitHub App token) with Contents, Issues and Pull requests read/write on the repository, store it as a secret (for example `OVERWATCH_TOKEN`), and pass it to both steps:

  ```yaml
      - uses: actions/checkout@v6
        with:
          token: ${{ secrets.OVERWATCH_TOKEN }}
          repository: ${{ github.event.pull_request.head.repo.full_name || github.repository }}
          ref: ${{ github.head_ref || github.ref }}
      - uses: your-real-username/repo-overwatch@v1
        with:
          github-token: ${{ secrets.OVERWATCH_TOKEN }}
          ...
  ```

## Configuration reference

**Workflow inputs** (`with:`): `gemini-api-key` (required), `anthropic-api-key` (optional), `github-token`, `branch` (set by the workflow), `gemini-model` (default `gemini-3.5-flash-lite`), `gemini-verify-model` (default: same as `gemini-model`), `verifier` (`auto`, `gemini` or `claude`), `gemini-rpm` (10), `gemini-tpm` (200000), `max-ai-requests` (100), `claude-model` (default `claude-sonnet-5`), `config-path` (default `.overwatch.yml`), `create-issues`, `create-fix-pr`, `max-new-issues` (20), `min-severity` (`low`), `fail-on` (`none`), `dry-run` (`false`).

**Outputs:** `findings-count`, `report-path`, `fix-pr-url`.

**`.overwatch.yml`:** see `templates/.overwatch.yml` for every key with comments. Values in this file override the workflow inputs.

To use a different model, change `gemini-model` / `claude-model` (or `gemini_model` / `claude_model` in `.overwatch.yml`) to any model ID your API key can access, for example `claude-opus-5-5` for the most thorough verification at a higher price.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Resource not accessible by integration` / 403 on issues | Part 4.2: set **Read and write permissions**, and keep the `permissions:` block in the workflow. |
| Report says GitHub Actions is not permitted to create pull requests | Part 4.2: tick **Allow GitHub Actions to create and approve pull requests**. |
| `Unable to resolve action your-real-username/repo-overwatch@v1` | The tag is missing (Part 3); or, for a private action repository, access isn't shared (Part 2, step 6); or the scanned repository is public, which cannot use a private action: make `repo-overwatch` public or follow Part 4b. |
| Part 4b: the download step fails with `Not Found` or a credentials error | The `OVERWATCH_ACTION_TOKEN` secret is missing, expired, or the token was not given access to `repo-overwatch` with Contents: Read-only. |
| Commenting `run overwatch` does nothing (no 👀 reaction) | The workflow file must be on the **default branch**; the comment must start with `run overwatch`; you must be the owner, a member or a collaborator. Check the Actions tab: a skipped run means one of these conditions failed. |
| `[run overwatch]` in a commit message does nothing | Only the **last** commit of a push is checked. |
| Report says AI analysis was skipped | The secrets are missing or misnamed (Part 4.1), or the run is a pull request from a fork. |
| Report says the scan **stopped early** | Normal on the free tier. Comment `run overwatch` again to continue; if the daily quota was used up, wait for the reset at midnight Pacific time. |
| It stops early on the very first request | The model has no free quota for your project. Set `gemini-model` back to `gemini-3.5-flash-lite` (or another Flash-Lite model shown in AI Studio). |
| Many "retrying" waits in the log | Your per-minute limit is lower than `gemini_rpm`; lower `gemini_rpm` (and `gemini_tpm`) in `.overwatch.yml`. |
| Error about an unknown model | The model was renamed or retired. Set `gemini-model` / `claude-model` to a current ID. |
| Fix PR not opened, report shows validation output | Your `validate:` commands failed with the fixes applied. The fixes are still described in each issue. |
| The run times out | Raise `timeout-minutes` in the workflow, narrow `include:`, or add `exclude:` patterns. Later runs are much faster thanks to the cache. |
| Overwatch commented that the scan failed | Open the linked run and expand the red step. A misspelled branch name in `run overwatch on …` fails at the checkout step. |
| Too many low-value issues | Set `min_severity: medium`, close noise as "not planned", or exclude generated code. |

## Limitations

- The import graph uses pattern matching rather than full compilers, so unusual import styles (dynamic paths, custom build aliases) may not be linked. Findings still work; only the linked-file hints are affected.
- Gitleaks scans the current files, not git history. Run `gitleaks git` separately for a one-time history audit.
- AI review is probabilistic. The verification pass filters many false positives, but a model double-checking its own family's work misses more of them than an independent reviewer would. Review every issue and fix PR critically, and close noise as "not planned" so it stays quiet.
- Free-tier quotas limit how much can be scanned per day; large repositories may need a few `run overwatch` commands (over one or more days) to finish their first full scan.
- Very large repositories are capped by `max_files` and `max_file_bytes`.

## Project layout

```
action.yml                  composite action definition
scripts/install-tools.sh    installs Gitleaks, OSV-Scanner, Semgrep (cached)
src/overwatch/
  config.py                 inputs, event context, .overwatch.yml
  repo.py                   file discovery, languages, outlines
  depgraph.py               linked-file graph, broken-link checker
  scanners.py               Semgrep, Gitleaks, OSV-Scanner
  analyzer.py               Gemini first pass (code and docs)
  verifier.py               verification pass (Gemini or Claude) and fixes
  llm.py                    Gemini/Claude clients, free-tier pacing, quota handling
  cache.py                  content-hash analysis cache
  issues.py                 issue sync, report issue, PR comment
  fixer.py                  patch application, validation, fix PR
  report.py                 Markdown rendering
  pipeline.py               orchestration
templates/overwatch.yml     on-command workflow to copy into watched repositories
templates/overwatch-auto.yml optional: scan on every push/PR/new branch instead
templates/overwatch-private-tool.yml  on-command workflow for a PUBLIC repo using a PRIVATE repo-overwatch
templates/.overwatch.yml    example per-repository config
tests/                      pytest suite
```

## License

MIT
