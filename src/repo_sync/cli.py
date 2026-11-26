from __future__ import annotations

import argparse
import subprocess
import sys
import shutil
import time
from pathlib import Path
from typing import Optional, Sequence, Tuple

from repo_sync import __version__
from repo_sync.github_sync import (
    GitHubClient,
    GitHubSyncError,
    config_path,
    initialize_config,
    load_config,
    run_github_watch,
    sync_forks,
)


TOP_LEVEL_GITHUB_COMMANDS = {"scan", "sync", "ask", "poll", "login", "whoami", "init"}


class RepoSyncError(Exception):
    """Expected operational error reported to the CLI user."""


class GitCommandError(RepoSyncError):
    def __init__(self, command: Sequence[str], stderr: str, remote_url: Optional[str] = None):
        message = stderr.strip() or "Git command failed."
        if remote_url:
            message = message.replace(remote_url, "<remote-url>")
        super().__init__(f"git {' '.join(command)} failed: {message}")


def run_git(
    args: Sequence[str],
    repo: Path,
    *,
    remote_url: Optional[str] = None,
    check: bool = True,
) -> str:
    """Run Git without a shell and return stdout."""
    completed = subprocess.run(
        ["git", *args],
        cwd=str(repo),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and completed.returncode != 0:
        raise GitCommandError(args, completed.stderr, remote_url)
    return completed.stdout.strip()


def resolve_repo(path_text: str) -> Path:
    repo = Path(path_text).expanduser().resolve()
    if not repo.exists() or not repo.is_dir():
        raise RepoSyncError(f"Repository path does not exist or is not a directory: {repo}")

    inside = run_git(["rev-parse", "--is-inside-work-tree"], repo, check=False)
    if inside != "true":
        raise RepoSyncError(f"Not inside a Git working tree: {repo}")
    return repo


def resolve_target(repo: Path, remote: str, branch_arg: Optional[str]) -> Tuple[str, str, str]:
    remote_url = run_git(["remote", "get-url", remote], repo, check=False)
    if not remote_url:
        raise RepoSyncError(f"Git remote '{remote}' was not found in {repo}")

    branch = branch_arg or run_git(
        ["symbolic-ref", "--quiet", "--short", "HEAD"], repo, check=False
    )
    if not branch:
        raise RepoSyncError(
            "Could not determine the current branch (the repository may be in detached-HEAD state). "
            "Pass --branch explicitly or check out a branch first."
        )
    return remote, branch, remote_url


def fetch(repo: Path, remote: str, branch: str, remote_url: str) -> None:
    run_git(["fetch", "--prune", remote, branch], repo, remote_url=remote_url)


def commit_distance(repo: Path) -> Tuple[int, int]:
    """Return (local_ahead, remote_ahead) relative to FETCH_HEAD."""
    counts = run_git(["rev-list", "--left-right", "--count", "HEAD...FETCH_HEAD"], repo)
    try:
        ahead_text, behind_text = counts.split()
        return int(ahead_text), int(behind_text)
    except (ValueError, TypeError):
        raise RepoSyncError(f"Could not understand Git commit counts: {counts!r}")


def describe_distance(ahead: int, behind: int) -> str:
    if ahead == 0 and behind == 0:
        return "Already up to date."
    if ahead == 0:
        return f"Update available: local branch is behind by {behind} commit(s)."
    if behind == 0:
        return f"Local branch is ahead by {ahead} commit(s); nothing to pull."
    return (
        f"Branches have diverged (local is ahead by {ahead} and behind by {behind} commit(s)). "
        "Manual reconciliation is required."
    )


def has_uncommitted_changes(repo: Path) -> bool:
    return bool(run_git(["status", "--porcelain"], repo))


def perform_check(repo: Path, remote: str, branch: Optional[str]) -> Tuple[int, int]:
    remote_name, target_branch, remote_url = resolve_target(repo, remote, branch)
    print(f"Fetching {remote_name}/{target_branch}...")
    fetch(repo, remote_name, target_branch, remote_url)
    ahead, behind = commit_distance(repo)
    print(describe_distance(ahead, behind))
    if has_uncommitted_changes(repo):
        print("Working tree has uncommitted changes; automatic updates are blocked.")
    return ahead, behind


def perform_update(repo: Path, remote: str, branch: Optional[str]) -> bool:
    if has_uncommitted_changes(repo):
        raise RepoSyncError(
            "Working tree is not clean. Commit or safely back up your changes before updating. "
            "repo-sync never stashes, resets, or overwrites local edits."
        )

    remote_name, target_branch, remote_url = resolve_target(repo, remote, branch)
    print(f"Fetching {remote_name}/{target_branch}...")
    fetch(repo, remote_name, target_branch, remote_url)
    ahead, behind = commit_distance(repo)

    if ahead > 0 and behind > 0:
        raise RepoSyncError(describe_distance(ahead, behind))
    if behind == 0:
        print(describe_distance(ahead, behind))
        return False

    # Fast-forward only: this cannot create a merge commit or discard local commits.
    run_git(["merge", "--ff-only", "FETCH_HEAD"], repo, remote_url=remote_url)
    new_head = run_git(["rev-parse", "--short", "HEAD"], repo)
    print(f"Updated to {remote_name}/{target_branch} ({new_head}).")
    return True


def add_repo_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("path", nargs="?", default=".", help="Local repository path (default: current directory)")
    parser.add_argument("--remote", default="origin", help="Git remote name (default: origin)")
    parser.add_argument("--branch", help="Remote branch to track (default: current local branch name)")


def add_github_common_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--desktop", action="store_true", help="Send desktop notifications")
    parser.add_argument("--webhook-url", help="Send notifications to an HTTPS webhook URL")


def add_github_watch_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--interval", type=int, default=300, help="Seconds between scans (minimum 30; default 300)")
    parser.add_argument("--dry-run", action="store_true", help="Detect changes without synchronizing forks")
    parser.add_argument("--no-sync", action="store_true", help="Only notify; do not synchronize forks")
    add_github_common_options(parser)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="repo-sync",
        description=(
            "Safely sync local Git checkouts or monitor all GitHub forks on your account. "
            "Run without a command to scan, review, and confirm a fork sync."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")

    # Short, top-level GitHub commands.
    scan_parser = commands.add_parser("scan", help="Scan forks and print a clean report (read-only)")
    add_github_common_options(scan_parser)

    sync_parser = commands.add_parser("sync", help="Sync every fork that is behind upstream")
    sync_parser.add_argument("--dry-run", action="store_true", help="Report actions without changing GitHub forks")
    add_github_common_options(sync_parser)

    ask_parser = commands.add_parser("ask", help="Scan, print a clean report, then ask before syncing")
    ask_parser.add_argument("--dry-run", action="store_true", help="Report actions without changing GitHub forks")
    add_github_common_options(ask_parser)

    poll_parser = commands.add_parser("poll", help="Poll GitHub and automatically sync changed forks")
    add_github_watch_options(poll_parser)

    commands.add_parser("login", help="Start GitHub CLI authentication, requesting the workflow scope")
    commands.add_parser("whoami", help="Show the GitHub account used by repo-sync")
    commands.add_parser("init", help="Create the user config file with safe defaults")

    # Local checkout commands.
    check_parser = commands.add_parser("check", help="Fetch and report whether a local branch needs an update")
    add_repo_arguments(check_parser)

    update_parser = commands.add_parser("update", help="Fast-forward a local repository when safe")
    add_repo_arguments(update_parser)

    watch_parser = commands.add_parser("watch", help="Watch one local repository and apply safe fast-forward updates")
    add_repo_arguments(watch_parser)
    watch_parser.add_argument("--interval", type=int, default=300, help="Seconds between checks (default: 300)")

    # Legacy `github <command>` namespace, kept for backwards compatibility.
    github_parser = commands.add_parser(
        "github", help="Legacy namespace for the top-level GitHub commands"
    )
    github_commands = github_parser.add_subparsers(dest="github_command", required=True)

    github_commands.add_parser("login", help="Start GitHub CLI authentication, requesting the workflow scope")
    github_commands.add_parser("whoami", help="Show the GitHub account used by repo-sync")
    github_commands.add_parser("init-config", help="Create the user config file with safe defaults")
    github_commands.add_parser("init", help="Alias for init-config")

    legacy_scan = github_commands.add_parser("scan", help="Alias for `repo-sync scan`")
    add_github_common_options(legacy_scan)

    legacy_sync = github_commands.add_parser("sync", help="Alias for `repo-sync sync`")
    legacy_sync.add_argument("--dry-run", action="store_true", help="Report actions without changing GitHub forks")
    add_github_common_options(legacy_sync)

    legacy_watch = github_commands.add_parser("watch", help="Alias for `repo-sync poll`")
    add_github_watch_options(legacy_watch)
    return parser


def _github_command_name(args: argparse.Namespace) -> str:
    """Map both the short commands and the legacy `github` namespace to one name."""
    if args.command is None:
        return "ask"
    if args.command == "github":
        name = getattr(args, "github_command", "") or "ask"
        return {"init-config": "init", "watch": "poll"}.get(name, name)
    return args.command


def _run_github_command(args: argparse.Namespace) -> int:
    command = _github_command_name(args)
    if command == "init":
        path = initialize_config()
        print(f"Created config: {path}")
        return 0
    if command == "login":
        if shutil.which("gh") is None:
            raise GitHubSyncError("GitHub CLI (gh) was not found. Install it, then rerun `repo-sync login`.")
        # The workflow scope is required for GitHub's merge-upstream endpoint to
        # update forks that contain .github/workflows files.
        completed = subprocess.run(
            ["gh", "auth", "login", "--scopes", "workflow"], check=False
        )
        return completed.returncode

    client = GitHubClient.from_gh_cli()
    if command == "whoami":
        print(f"Authenticated GitHub account: @{client.get_account_login()}")
        return 0

    config = load_config()
    desktop = bool(config.get("desktop_notifications", False) or getattr(args, "desktop", False))
    webhook_url = getattr(args, "webhook_url", None) or str(config.get("webhook_url") or "")

    if command == "scan":
        sync_forks(
            client, config=config, do_sync=False, desktop=desktop, webhook_url=webhook_url
        )
        return 0
    if command == "sync":
        _, _, errors = sync_forks(
            client,
            config=config,
            do_sync=True,
            dry_run=bool(getattr(args, "dry_run", False)),
            desktop=desktop,
            webhook_url=webhook_url,
        )
        return 1 if errors else 0
    if command == "ask":
        _, _, errors = sync_forks(
            client,
            config=config,
            do_sync=True,
            dry_run=bool(getattr(args, "dry_run", False)),
            desktop=desktop,
            webhook_url=webhook_url,
            confirm=True,
        )
        return 1 if errors else 0
    if command == "poll":
        return run_github_watch(
            interval=args.interval,
            do_sync=not args.no_sync,
            dry_run=args.dry_run,
            config=config,
            desktop=desktop,
            webhook_url=webhook_url,
        )
    return 2


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command is None or args.command == "github" or args.command in TOP_LEVEL_GITHUB_COMMANDS:
            return _run_github_command(args)

        repo = resolve_repo(args.path)
        if args.command == "check":
            perform_check(repo, args.remote, args.branch)
            return 0
        if args.command == "update":
            perform_update(repo, args.remote, args.branch)
            return 0
        if args.command == "watch":
            if args.interval < 1:
                raise RepoSyncError("--interval must be at least 1 second.")
            print(f"Watching {repo} every {args.interval} second(s). Press Ctrl+C to stop.")
            try:
                while True:
                    perform_update(repo, args.remote, args.branch)
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                print("\nStopped watching.")
                return 0
        parser.error("Unknown command")
    except (RepoSyncError, GitHubSyncError) as exc:
        print(f"repo-sync: error: {exc}", file=sys.stderr)
        return 1
    except FileNotFoundError:
        print("repo-sync: error: Git was not found. Install Git and make sure it is on PATH.", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
