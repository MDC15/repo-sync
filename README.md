# repo-sync

`repo-sync` can do two jobs:

1. Monitor and safely fast-forward a **single local Git checkout** from one of its remotes.
2. Connect to your **GitHub account**, discover its accessible fork repositories (including read-only forks), detect when their original (upstream) repositories gain commits, notify you, and synchronize writable forks on GitHub.

The GitHub account is authenticated through the local GitHub CLI (`gh`). No access token is written to the `repo-sync` config file.

## Install from source

On macOS or Linux, run the bootstrap script from the project directory. It creates a local `.venv`, installs build tools, runs the unit tests, builds a wheel into `dist/`, and installs that wheel into the virtual environment.

```bash
chmod +x setup.sh
./setup.sh

# Activate the environment in a new terminal session
source .venv/bin/activate
repo-sync --help
```

You can select another Python executable or virtualenv path: `PYTHON=python3.12 REPO_SYNC_VENV=.venv312 ./setup.sh`. Set `REPO_SYNC_SKIP_TESTS=1` to skip the unit tests.

## Requirements

- Python 3.9+
- Git installed and available on `PATH` (for local checkout commands)
- GitHub CLI installed: <https://cli.github.com/>

## Connect your GitHub account

```bash
gh auth login --scopes workflow
# Or start the same login flow through repo-sync:
repo-sync github login

repo-sync github whoami
```

For private repositories, the GitHub CLI session must have permissions to read the account's repositories and write repository contents on the forks you want to sync. If GitHub reports an authorization error, review the token permissions in `gh auth status` and refresh the needed access.

GitHub's fork-sync endpoint also refuses to create or update files under `.github/workflows/` unless the token carries the `workflow` scope. Most forks of active projects contain such files, so grant the scope once:

```bash
gh auth refresh -h github.com -s workflow
```

`repo-sync github login` requests the `workflow` scope for new sessions, and `repo-sync github sync` warns when an existing session lacks it. Without the scope, affected forks fail with HTTP 422 (`... without \`workflow\` scope`) and the error message tells you which command to run.

## Commands at a glance

```bash
repo-sync                  # scan, show a clean report, then ask y/n before syncing
repo-sync scan             # scan only (read-only)
repo-sync sync             # sync every fork that is behind upstream
repo-sync poll             # poll and auto-sync changed forks
repo-sync login            # authenticate gh and request the workflow scope
repo-sync whoami           # show the connected account
repo-sync init             # create the config file
```

The older `repo-sync github <command>` names still work (`github scan`, `github sync`, `github watch`, `github login`, `github whoami`, `github init-config`).

## Scan forks

```bash
# Read-only: list forks and report which upstreams have new commits.
repo-sync scan

# Show the connected account.
repo-sync whoami
```

The report lists only the forks that need attention, aligned in columns, with a one-line summary for everything else:

```
GitHub forks — @your-name

3 fork(s) behind upstream:
  you/project-a  upstream/project-a  ↓ 12 commit(s)
  you/project-b  upstream/project-b  ↓ 4 commit(s)
  you/project-c  upstream/project-c  ↓ 1 commit(s)

  ✓ 16 up to date   ↓ 3 to sync
```

The scanner uses GitHub repository search with the explicit `user:<login> fork:only` qualifier, then fetches full metadata for each result to obtain its upstream (`parent`) and permission fields. This avoids relying on repository-list responses that may omit fork-specific metadata. Results are paginated. It includes archived and read-only forks in discovery by default so they are not silently missed. Read-only and archived forks are reported but never synchronized. Set `include_archived` to `false` in the config if you want to hide archived forks. Each fork's default branch is compared with its parent's default branch; mismatched branch names are reported instead of guessed.

## Review and sync interactively

```bash
# Scan, show the report, and ask before changing anything.
repo-sync

# Same thing, explicitly.
repo-sync ask
```

`repo-sync ask` prints the same clean report, then prompts:

```
Sync these 3 fork(s) now? [y/N]:
```

Answer `y` to sync all listed forks, or press Enter / `n` to cancel without changing anything. After syncing it prints one line per repository and a final result summary.

## Automatically synchronize changed forks

```bash
# One scan; sync every fork that is behind its upstream.
repo-sync sync

# Preview without changing any repositories.
repo-sync sync --dry-run

# Poll every five minutes, notify, and sync automatically.
repo-sync poll --interval 300

# Poll and notify, but do not sync automatically.
repo-sync poll --interval 300 --no-sync

# Also request native desktop notifications.
repo-sync poll --interval 300 --desktop
```

`poll` polls GitHub; it is not an instant webhook. The minimum interval is 30 seconds. The default is 300 seconds. Stop it with `Ctrl+C`.

A successful sync updates the fork on GitHub using GitHub's **Sync a fork branch** API. The operation does not force-push or reset a branch. If upstream and fork history cannot be reconciled, GitHub rejects the sync and the tool reports the failure.

## Notifications

Terminal notifications are always printed. Optional desktop notifications are supported on macOS, Linux (with `notify-send`), and Windows. You can also configure an HTTPS webhook URL for Slack-compatible or Discord-compatible incoming webhooks.

Create the config file:

```bash
repo-sync init
```

Default location: `~/.config/repo-sync/config.json`.

Example:

```json
{
  "exclude_repos": ["my-account/old-fork"],
  "include_repos": [],
  "include_archived": false,
  "branch_overrides": {},
  "desktop_notifications": true,
  "webhook_url": "",
  "after_sync": {
    "my-account/my-project-fork": [
      ["repo-sync", "update", "/work/my-project-fork", "--remote", "origin", "--branch", "main"],
      ["npm", "test"]
    ],
    "*": []
  }
}
```

- `exclude_repos`: fork repositories to ignore (`owner/name`).
- `include_repos`: optional allowlist; when empty, all accessible forks are considered, including read-only forks.
- `include_archived`: whether archived forks are included in discovery (default: `true`). Archived forks are never synchronized.
- `branch_overrides`: map `owner/name` to a branch name that exists on both the fork and its upstream. This is useful when you want to monitor a non-default branch. The tool will skip branches with mismatched names rather than guessing.
- `desktop_notifications`: enable native notifications by default.
- `webhook_url`: optional HTTPS incoming-webhook endpoint. Treat it as a secret; keep the config file private.
- `after_sync`: optional argument arrays to run after the GitHub fork has synchronized. Use a repository key or `*` for a default. Commands are executed without a shell; use `bash -lc` explicitly only when a compound shell command is needed. These commands are your own trusted local commands, not code supplied by upstream repositories.

The `{repo}`, `{upstream}`, and `{branch}` placeholders are available in hook arguments. Example:

```json
"after_sync": {
  "*": [["echo", "Updated {repo} from {upstream} on {branch}"]]
}
```

## Local checkout commands

The original local-repository workflow remains available:

```bash
repo-sync check /path/to/checkout
repo-sync update /path/to/checkout
repo-sync watch /path/to/checkout --interval 300
```

`watch` here watches a **local checkout**. For GitHub forks, use `repo-sync poll`.

These commands fetch a configured Git remote and update only when Git can perform a fast-forward on a clean working tree.

## Safety behavior

- Never uses `git reset --hard`, force-push, or automatic stash.
- Local checkout updates require a clean working tree and use `git merge --ff-only`.
- GitHub fork sync uses GitHub's sync endpoint, which can fast-forward or create a normal merge when possible; conflicts are reported and are not force-resolved.
- Hooks run only when explicitly configured by the local user. They are executed as argument arrays, not shell strings.
- Authentication tokens are read from `gh auth token` at runtime and are not written to the config or logs.
- `--dry-run` does not modify GitHub repositories.

## Troubleshooting

- `HTTP 422: refusing to allow an OAuth App to create or update workflow ... without workflow scope` — the GitHub CLI token lacks the `workflow` scope. Run `gh auth refresh -h github.com -s workflow`, then retry. `repo-sync sync` also prints a warning up front when it detects this.
- `HTTP 409` — GitHub could not sync the branch, usually because upstream and fork history conflict. Resolve the conflict in the fork, then retry.
- `~ ... branch ... differs from upstream` — the fork and upstream use different default branch names. Add a `branch_overrides` entry only when both repositories have a branch with the same name.

## Development

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
```
