from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple


API_VERSION = "2022-11-28"
API_BASE = "https://api.github.com"
USER_AGENT = "repo-sync-cli"


class GitHubSyncError(Exception):
    """Expected GitHub authentication, API, or sync error."""


@dataclass
class ForkStatus:
    full_name: str
    upstream_full_name: str
    branch: str
    upstream_branch: str
    ahead_by: int
    behind_by: int
    status: str
    html_url: str
    upstream_sha: str = ""
    error: str = ""
    writable: bool = True
    archived: bool = False

    @property
    def update_available(self) -> bool:
        return self.ahead_by > 0


class GitHubClient:
    """Small GitHub REST client authenticated using the local GitHub CLI session."""

    def __init__(self, token: str, api_base: str = API_BASE) -> None:
        self._token = token.strip()
        if not self._token:
            raise GitHubSyncError("GitHub CLI returned an empty authentication token.")
        self.api_base = api_base.rstrip("/")
        # Populated from the X-OAuth-Scopes response header. Stays None for token
        # types (such as fine-grained PATs) that do not expose scopes.
        self.scopes: Optional[set] = None

    @classmethod
    def from_gh_cli(cls) -> "GitHubClient":
        if shutil.which("gh") is None:
            raise GitHubSyncError(
                "GitHub CLI (gh) was not found. Install it, then run `gh auth login`."
            )
        try:
            result = subprocess.run(
                ["gh", "auth", "token", "--hostname", "github.com"],
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise GitHubSyncError(f"Could not start GitHub CLI: {exc}")
        if result.returncode != 0 or not result.stdout.strip():
            detail = result.stderr.strip() or "No authenticated GitHub session was found."
            raise GitHubSyncError(
                f"{detail}\nAuthenticate first with: gh auth login"
            )
        return cls(result.stdout)

    def request(
        self,
        method: str,
        path: str,
        body: Optional[Dict[str, Any]] = None,
    ) -> Tuple[Any, Dict[str, str]]:
        url = path if path.startswith("https://") else f"{self.api_base}{path}"
        payload = None if body is None else json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            url,
            data=payload,
            method=method.upper(),
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self._token}",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
                **({"Content-Type": "application/json"} if payload is not None else {}),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read().decode("utf-8")
                data = json.loads(raw) if raw else None
                headers = {key.lower(): value for key, value in response.headers.items()}
                self._record_scopes(headers)
                return data, headers
        except urllib.error.HTTPError as exc:
            self._record_scopes(exc.headers)
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(raw).get("message", raw)
            except (ValueError, AttributeError):
                detail = raw or str(exc.reason)
            if exc.code == 401:
                hint = " Re-authenticate with `gh auth login`."
            elif exc.code == 403:
                hint = " Check token permissions and GitHub API rate limits."
            elif exc.code == 404:
                hint = " The repository may be private or unavailable to this account."
            elif exc.code == 409:
                hint = " GitHub could not sync this branch, usually because of merge conflicts."
            elif exc.code == 422 and "workflow" in str(detail).lower():
                hint = (
                    " The GitHub CLI token is missing the `workflow` scope, which GitHub"
                    " requires before it will create or update files under .github/workflows/."
                    " Grant it once with `gh auth refresh -h github.com -s workflow`, then retry."
                )
            else:
                hint = ""
            raise GitHubSyncError(f"GitHub API returned HTTP {exc.code}: {detail}.{hint}")
        except urllib.error.URLError as exc:
            raise GitHubSyncError(f"Could not connect to GitHub: {exc.reason}")
        except TimeoutError:
            raise GitHubSyncError("GitHub API request timed out.")

    def get(self, path: str) -> Any:
        return self.request("GET", path)[0]

    def post(self, path: str, body: Dict[str, Any]) -> Any:
        return self.request("POST", path, body)[0]

    def _record_scopes(self, headers: Any) -> None:
        """Capture OAuth scopes from a response header when GitHub reports them."""
        value = None
        if headers is not None:
            try:
                value = headers.get("x-oauth-scopes")
                if value is None:
                    value = headers.get("X-OAuth-Scopes")
            except AttributeError:
                value = None
        if value is None:
            return
        self.scopes = {item.strip() for item in str(value).split(",") if item.strip()}

    def has_scope(self, scope: str) -> Optional[bool]:
        """Return whether the token has a scope; None when scopes are not exposed."""
        if self.scopes is None:
            return None
        return scope in self.scopes

    def get_all_repositories(self) -> List[Dict[str, Any]]:
        """List repositories accessible to the account, following REST pagination."""
        path = "/user/repos?per_page=100&sort=updated&affiliation=owner%2Ccollaborator%2Corganization_member"
        repositories: List[Dict[str, Any]] = []
        while path:
            page, headers = self.request("GET", path)
            if not isinstance(page, list):
                raise GitHubSyncError("Unexpected response while listing GitHub repositories.")
            repositories.extend(page)
            path = _next_link(headers.get("link", ""))
        return repositories

    def get_account_login(self) -> str:
        user = self.get("/user")
        login = user.get("login") if isinstance(user, dict) else None
        if not login:
            raise GitHubSyncError("GitHub did not return an account login.")
        return str(login)

    def get_all_fork_repositories(self, login: str) -> List[Dict[str, Any]]:
        """Discover account forks through GitHub Search's explicit fork:only filter."""
        query = urllib.parse.urlencode({
            "q": f"user:{login} fork:only",
            "per_page": 100,
            "page": 1,
        })
        path = f"/search/repositories?{query}"
        repositories: List[Dict[str, Any]] = []
        while path:
            page, headers = self.request("GET", path)
            if not isinstance(page, dict) or not isinstance(page.get("items"), list):
                raise GitHubSyncError("Unexpected response while searching GitHub fork repositories.")
            repositories.extend(page["items"])
            path = _next_link(headers.get("link", ""))
        return repositories

    def get_repository_details(self, full_name: str) -> Dict[str, Any]:
        """Fetch full repository metadata, including parent/source and permissions."""
        if "/" not in full_name:
            raise GitHubSyncError(f"Invalid GitHub repository name: {full_name!r}")
        owner, name = full_name.split("/", 1)
        path = (
            f"/repos/{urllib.parse.quote(owner, safe='')}/"
            f"{urllib.parse.quote(name, safe='')}"
        )
        details = self.get(path)
        if not isinstance(details, dict):
            raise GitHubSyncError(f"GitHub returned invalid metadata for {full_name}.")
        return details

    def scan_forks(
        self,
        *,
        include_archived: bool = True,
        excluded_repositories: Optional[Iterable[str]] = None,
        included_repositories: Optional[Iterable[str]] = None,
        branch_overrides: Optional[Dict[str, str]] = None,
    ) -> List[ForkStatus]:
        excluded = {item.lower() for item in (excluded_repositories or [])}
        included = {item.lower() for item in (included_repositories or [])}
        overrides = {key.lower(): value for key, value in (branch_overrides or {}).items()}
        result: List[ForkStatus] = []
        login = self.get_account_login()
        fork_candidates = self.get_all_fork_repositories(login)

        for candidate in fork_candidates:
            full_name = str(candidate.get("full_name") or "")
            if not full_name:
                continue
            if full_name.lower() in excluded:
                continue
            if included and full_name.lower() not in included:
                continue

            # Search results are optimized for discovery and may omit fields required
            # for synchronization. Hydrate each result through GET /repos/{owner}/{repo}.
            try:
                repo = self.get_repository_details(full_name)
            except GitHubSyncError as exc:
                result.append(
                    ForkStatus(
                        full_name=full_name, upstream_full_name="", branch="",
                        upstream_branch="", ahead_by=0, behind_by=0, status="error",
                        html_url=str(candidate.get("html_url") or f"https://github.com/{full_name}"),
                        error=f"Could not load fork metadata: {exc}", writable=False,
                        archived=bool(candidate.get("archived", False)),
                    )
                )
                continue

            # The search query is fork:only, but retain this guard against unexpected
            # or malformed search results. Missing parent metadata is an error, not a
            # reason to silently drop a discovered fork.
            if repo.get("fork") is False:
                continue
            parent = repo.get("parent")
            if not isinstance(parent, dict):
                result.append(
                    ForkStatus(
                        full_name=full_name, upstream_full_name="",
                        branch=str(repo.get("default_branch") or ""), upstream_branch="",
                        ahead_by=0, behind_by=0, status="error",
                        html_url=str(repo.get("html_url") or f"https://github.com/{full_name}"),
                        error="GitHub identified this repository as a fork but did not return its parent metadata.",
                        writable=False, archived=bool(repo.get("archived", False)),
                    )
                )
                continue

            archived = bool(repo.get("archived", False))
            if archived and not include_archived:
                continue
            # Discovery and synchronization are separate concerns: include read-only
            # forks in scan results, but never attempt to sync them.
            permissions = repo.get("permissions") or {}
            writable = bool(permissions.get("push", False)) if permissions else False
            writable = writable and not archived

            upstream_full_name = str(parent.get("full_name") or "")
            if not upstream_full_name:
                result.append(
                    ForkStatus(
                        full_name=full_name, upstream_full_name="",
                        branch=str(repo.get("default_branch") or ""), upstream_branch="",
                        ahead_by=0, behind_by=0, status="error",
                        html_url=str(repo.get("html_url") or f"https://github.com/{full_name}"),
                        error="GitHub returned parent metadata without a full_name.",
                        writable=writable, archived=archived,
                    )
                )
                continue
            configured_branch = overrides.get(full_name.lower())
            branch = configured_branch or str(repo.get("default_branch") or "")
            upstream_branch = configured_branch or str(parent.get("default_branch") or branch)
            if not branch:
                result.append(
                    ForkStatus(
                        full_name=full_name, upstream_full_name=upstream_full_name,
                        branch="", upstream_branch=upstream_branch, ahead_by=0, behind_by=0,
                        status="error", html_url=str(repo.get("html_url") or f"https://github.com/{full_name}"),
                        error="Could not determine the fork's default branch.",
                        writable=writable, archived=archived,
                    )
                )
                continue

            # GitHub's sync-fork endpoint updates the named branch from its
            # corresponding upstream branch. Do not guess when names differ.
            if branch != upstream_branch:
                result.append(
                    ForkStatus(
                        full_name, upstream_full_name, branch, upstream_branch,
                        0, 0, "branch-mismatch", str(repo.get("html_url") or ""),
                        writable=writable, archived=archived,
                    )
                )
                continue

            owner, name = full_name.split("/", 1)
            upstream_owner = parent.get("owner") or {}
            upstream_login = upstream_owner.get("login") or upstream_full_name.split("/", 1)[0]
            compare_head = f"{upstream_login}:{upstream_branch}"
            base_ref = urllib.parse.quote(branch, safe="")
            head_ref = urllib.parse.quote(compare_head, safe="")
            compare_path = (
                f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(name, safe='')}"
                f"/compare/{base_ref}...{head_ref}"
            )
            try:
                compare = self.get(compare_path)
            except GitHubSyncError as exc:
                result.append(
                    ForkStatus(
                        full_name=full_name, upstream_full_name=upstream_full_name,
                        branch=branch, upstream_branch=upstream_branch, ahead_by=0,
                        behind_by=0, status="error",
                        html_url=str(repo.get("html_url") or f"https://github.com/{full_name}"),
                        error=str(exc), writable=writable, archived=archived,
                    )
                )
                continue
            result.append(
                ForkStatus(
                    full_name=full_name,
                    upstream_full_name=upstream_full_name,
                    branch=branch,
                    upstream_branch=upstream_branch,
                    ahead_by=int(compare.get("ahead_by", 0)),
                    behind_by=int(compare.get("behind_by", 0)),
                    status=str(compare.get("status", "unknown")),
                    html_url=str(repo.get("html_url") or f"https://github.com/{full_name}"),
                    upstream_sha=str((compare.get("head_commit") or {}).get("sha") or ""),
                    writable=writable, archived=archived,
                )
            )
        return result

    def sync_fork(self, status: ForkStatus) -> Dict[str, Any]:
        owner, name = status.full_name.split("/", 1)
        path = (
            f"/repos/{urllib.parse.quote(owner, safe='')}/"
            f"{urllib.parse.quote(name, safe='')}/merge-upstream"
        )
        return self.post(path, {"branch": status.branch})


def _next_link(link_header: str) -> str:
    """Return the URL for the Link header's rel=next entry, if present."""
    for part in link_header.split(","):
        section = part.strip()
        if 'rel="next"' in section:
            left = section.find("<")
            right = section.find(">", left + 1)
            if left >= 0 and right > left:
                return section[left + 1 : right]
    return ""


def config_dir() -> Path:
    return Path.home() / ".config" / "repo-sync"


def config_path() -> Path:
    return config_dir() / "config.json"


def state_path() -> Path:
    return config_dir() / "state.json"


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    location = path or config_path()
    if not location.exists():
        return {}
    try:
        with location.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise GitHubSyncError(f"Could not read config {location}: {exc}")
    if not isinstance(loaded, dict):
        raise GitHubSyncError(f"Config must contain a JSON object: {location}")
    return loaded


def initialize_config(path: Optional[Path] = None) -> Path:
    location = path or config_path()
    if location.exists():
        raise GitHubSyncError(f"Config already exists: {location}")
    location.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(location.parent, 0o700)
    except OSError:
        pass
    sample = {
        "exclude_repos": [],
        "include_repos": [],
        "include_archived": True,
        "branch_overrides": {},
        "desktop_notifications": False,
        "webhook_url": "",
        "after_sync": {},
    }
    location.write_text(json.dumps(sample, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(location, 0o600)
    except OSError:
        pass
    return location


def load_state(path: Optional[Path] = None) -> Dict[str, Any]:
    location = path or state_path()
    if not location.exists():
        return {}
    try:
        loaded = json.loads(location.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_state(state: Dict[str, Any], path: Optional[Path] = None) -> None:
    location = path or state_path()
    location.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(location.parent, 0o700)
    except OSError:
        pass
    temporary = location.with_suffix(location.suffix + ".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(temporary, 0o600)
    except OSError:
        pass
    os.replace(str(temporary), str(location))


def notify(
    title: str,
    message: str,
    *,
    desktop: bool = False,
    webhook_url: str = "",
    terminal: bool = True,
) -> None:
    if terminal:
        print(f"\n[repo-sync] {title}: {message}", flush=True)
    if desktop:
        _desktop_notify(title, message)
    if webhook_url:
        _webhook_notify(webhook_url, title, message)


def _desktop_notify(title: str, message: str) -> None:
    system = platform.system()
    try:
        if system == "Darwin" and shutil.which("osascript"):
            subprocess.run(
                ["osascript", "-e", "display notification " + _applescript_string(message)
                 + " with title " + _applescript_string(title)],
                check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif system == "Linux" and shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", title, message], check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        elif system == "Windows" and shutil.which("powershell"):
            escaped_title = title.replace("'", "''")
            escaped_message = message.replace("'", "''")
            script = (
                "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
                "$template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02;"
                "$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($template);"
                f"$xml.GetElementsByTagName('text')[0].AppendChild($xml.CreateTextNode('{escaped_title}')) > $null;"
                f"$xml.GetElementsByTagName('text')[1].AppendChild($xml.CreateTextNode('{escaped_message}')) > $null;"
                "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml);"
                "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('repo-sync').Show($toast);"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", script], check=False,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
    except OSError:
        # Terminal output remains available if the desktop notifier is missing.
        pass


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _webhook_notify(webhook_url: str, title: str, message: str) -> None:
    parsed = urllib.parse.urlparse(webhook_url)
    if parsed.scheme != "https" or not parsed.netloc:
        print("[repo-sync] warning: webhook URL must use HTTPS; skipped.", file=sys.stderr)
        return
    payload = json.dumps({"text": f"{title}: {message}", "content": f"{title}: {message}"}).encode("utf-8")
    request = urllib.request.Request(
        webhook_url,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=10):
            pass
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"[repo-sync] warning: webhook notification failed: {exc}", file=sys.stderr)


def _after_sync_commands(config: Dict[str, Any], repo_full_name: str) -> List[List[str]]:
    hooks = config.get("after_sync", {})
    if isinstance(hooks, list):
        raw_commands: Any = hooks
    elif isinstance(hooks, dict):
        raw_commands = hooks.get(repo_full_name, hooks.get("*", []))
    else:
        raw_commands = []
    if not isinstance(raw_commands, list):
        raise GitHubSyncError(f"after_sync for {repo_full_name} must be a list of command arrays.")
    commands: List[List[str]] = []
    for command in raw_commands:
        if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
            raise GitHubSyncError(
                f"Invalid after_sync command for {repo_full_name}; use arrays, e.g. [\"npm\", \"test\"]."
            )
        commands.append(command)
    return commands


def run_after_sync(config: Dict[str, Any], status: ForkStatus) -> List[int]:
    exit_codes: List[int] = []
    values = {
        "repo": status.full_name,
        "upstream": status.upstream_full_name,
        "branch": status.branch,
    }
    for raw_command in _after_sync_commands(config, status.full_name):
        command = [
            arg.replace("{repo}", values["repo"])
               .replace("{upstream}", values["upstream"])
               .replace("{branch}", values["branch"])
            for arg in raw_command
        ]
        print(f"[repo-sync] post-sync command: {command}", flush=True)
        try:
            completed = subprocess.run(command, check=False)
            exit_codes.append(completed.returncode)
            if completed.returncode != 0:
                print(
                    f"[repo-sync] warning: post-sync command exited with {completed.returncode}.",
                    file=sys.stderr,
                )
        except OSError as exc:
            exit_codes.append(127)
            print(f"[repo-sync] warning: could not run post-sync command: {exc}", file=sys.stderr)
    return exit_codes


def _supports_color(stream: Any) -> bool:
    return bool(
        hasattr(stream, "isatty")
        and stream.isatty()
        and not os.environ.get("NO_COLOR")
    )


def _paint(text: str, code: str, color: bool) -> str:
    return f"\033[{code}m{text}\033[0m" if color else text


def format_fork_report(
    statuses: Sequence[ForkStatus],
    *,
    login: str = "",
    color: bool = False,
) -> str:
    """Render a compact, human-readable summary of a fork scan."""
    actionable = [
        item for item in statuses if item.status not in ("error", "branch-mismatch")
    ]
    behind = [item for item in actionable if item.update_available]
    pending = [item for item in behind if item.writable]
    up_to_date = [item for item in actionable if not item.update_available]
    read_only = [item for item in behind if not item.writable]
    mismatched = [item for item in statuses if item.status == "branch-mismatch"]
    errored = [item for item in statuses if item.status == "error"]

    lines: List[str] = []
    heading = "GitHub forks"
    if login:
        heading += f" — @{login}"
    lines.append(_paint(heading, "1", color))
    lines.append("")

    if pending:
        lines.append(_paint(f"{len(pending)} fork(s) behind upstream:", "33", color))
        name_width = max(len(item.full_name) for item in pending)
        upstream_labels = [
            f"{item.upstream_full_name}/{item.upstream_branch}" for item in pending
        ]
        upstream_width = max(len(label) for label in upstream_labels)
        for item, upstream in zip(pending, upstream_labels):
            count = f"{item.ahead_by} commit(s)"
            lines.append(
                f"  {item.full_name.ljust(name_width)}  "
                f"{upstream.ljust(upstream_width)}  "
                f"{_paint('↓ ' + count, '33', color)}"
            )
        lines.append("")
    elif behind:
        lines.append(
            _paint(
                f"{len(behind)} fork(s) are behind upstream, but none are writable.",
                "2",
                color,
            )
        )
        lines.append("")
    else:
        lines.append(_paint("  ✓ All forks are up to date.", "32", color))
        lines.append("")

    summary: List[str] = []
    if up_to_date:
        summary.append(_paint(f"✓ {len(up_to_date)} up to date", "32", color))
    if pending:
        summary.append(_paint(f"↓ {len(pending)} to sync", "33", color))
    if read_only:
        summary.append(_paint(f"⊘ {len(read_only)} read-only", "2", color))
    if mismatched:
        summary.append(_paint(f"~ {len(mismatched)} branch mismatch", "33", color))
    if errored:
        summary.append(_paint(f"! {len(errored)} error(s)", "31", color))
    if summary:
        lines.append("  " + "   ".join(summary))

    for item in read_only:
        suffix = " (archived)" if item.archived else ""
        lines.append(
            _paint(
                f"  ⊘ {item.full_name}: read-only, cannot sync{suffix}",
                "2",
                color,
            )
        )
    for item in mismatched:
        lines.append(
            _paint(
                f"  ~ {item.full_name}: branch '{item.branch}' differs from "
                f"upstream '{item.upstream_branch}'",
                "33",
                color,
            )
        )
    for item in errored:
        lines.append(_paint(f"  ! {item.full_name}: {item.error}", "31", color))
    return "\n".join(lines)


def prompt_yes_no(
    question: str,
    *,
    default: bool = False,
    input_fn: Callable[[str], str] = input,
) -> bool:
    """Ask a yes/no question; empty input, EOF, and Ctrl+C use the default."""
    try:
        answer = input_fn(question).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    if not answer:
        return default
    return answer in ("y", "yes", "c", "co", "có")


def _short_error(message: str) -> str:
    lowered = message.lower()
    if "workflow" in lowered and "scope" in lowered:
        return "missing `workflow` scope (run: gh auth refresh -h github.com -s workflow)"
    single_line = " ".join(message.split())
    if len(single_line) > 120:
        return single_line[:117] + "..."
    return single_line


def _sync_one_fork(
    client: GitHubClient,
    item: ForkStatus,
    *,
    config: Dict[str, Any],
    desktop: bool,
    webhook_url: str,
    state: Dict[str, Any],
    state_file: Optional[Path],
    announce: bool,
) -> Tuple[bool, int, str]:
    """Sync one fork. Returns (success, extra_error_count, detail)."""
    try:
        response = client.sync_fork(item)
    except GitHubSyncError as exc:
        message = str(exc)
        if announce:
            failure = f"Could not sync {item.full_name}: {exc}"
            print(f"ERROR {failure}", file=sys.stderr)
            notify("Fork sync failed", failure, desktop=desktop, webhook_url=webhook_url)
        return False, 0, message

    merge_type = (
        str(response.get("merge_type", "synced"))
        if isinstance(response, dict)
        else "synced"
    )
    if announce:
        success_message = f"{item.full_name} synced ({merge_type})."
        print(f"DONE  {success_message}")
        notify(
            "Fork synchronized",
            success_message,
            desktop=desktop,
            webhook_url=webhook_url,
        )

    hook_errors = 0
    try:
        run_after_sync(config, item)
    except GitHubSyncError as exc:
        hook_errors = 1
        print(
            f"[repo-sync] post-sync hook configuration error for {item.full_name}: {exc}",
            file=sys.stderr,
        )

    existing = state.get(item.full_name)
    state[item.full_name] = {
        **(existing if isinstance(existing, dict) else {}),
        "notified_upstream": None,
    }
    save_state(state, state_file)
    return True, hook_errors, merge_type


def sync_forks(
    client: GitHubClient,
    *,
    config: Dict[str, Any],
    do_sync: bool,
    dry_run: bool = False,
    desktop: bool = False,
    webhook_url: str = "",
    state_file: Optional[Path] = None,
    confirm: bool = False,
    input_fn: Callable[[str], str] = input,
    stream: Optional[Any] = None,
) -> Tuple[int, int, int]:
    """Scan forks, print a compact report, and optionally synchronize them.

    Returns ``(behind_count, synced_count, error_count)``. With ``confirm=True``
    the user is asked y/n before any repository is modified.
    """
    output = stream if stream is not None else sys.stdout
    color = _supports_color(output)
    statuses = client.scan_forks(
        include_archived=bool(config.get("include_archived", True)),
        excluded_repositories=config.get("exclude_repos", []),
        included_repositories=config.get("include_repos", []),
        branch_overrides=config.get("branch_overrides", {}),
    )
    try:
        login = client.get_account_login()
    except GitHubSyncError:
        login = ""
    print(format_fork_report(statuses, login=login, color=color), file=output)

    actionable = [
        item for item in statuses if item.status not in ("error", "branch-mismatch")
    ]
    behind = [item for item in actionable if item.update_available]
    pending = [item for item in behind if item.writable]
    errors = sum(1 for item in statuses if item.status == "error")

    if do_sync and client.has_scope("workflow") is False:
        print(
            "[repo-sync] warning: the GitHub CLI token lacks the `workflow` scope. GitHub "
            "rejects syncs for forks whose upstream adds or changes files under "
            ".github/workflows/. Grant it once with: "
            "gh auth refresh -h github.com -s workflow",
            file=sys.stderr,
        )

    state = load_state(state_file)
    for item in behind:
        marker = item.upstream_sha or f"{item.status}:{item.ahead_by}"
        existing = state.get(item.full_name)
        existing = existing if isinstance(existing, dict) else {}
        if existing.get("notified_upstream") == marker:
            continue
        notify(
            "Upstream changes detected",
            f"{item.full_name} has {item.ahead_by} upstream commit(s) to sync from "
            f"{item.upstream_full_name}/{item.upstream_branch}. {item.html_url}",
            desktop=desktop,
            webhook_url=webhook_url,
            terminal=False,
        )
        state[item.full_name] = {**existing, "notified_upstream": marker}
        save_state(state, state_file)

    if not do_sync or not pending:
        return len(behind), 0, errors

    if dry_run:
        print(f"\nDRY-RUN: would sync {len(pending)} fork(s).", file=output)
        return len(behind), 0, errors

    if confirm:
        if not prompt_yes_no(
            f"\nSync these {len(pending)} fork(s) now? [y/N]: ", input_fn=input_fn
        ):
            print("Cancelled. No repositories were changed.", file=output)
            return len(behind), 0, errors

    print("", file=output)
    synced = 0
    failed = 0
    for item in pending:
        ok, hook_errors, detail = _sync_one_fork(
            client,
            item,
            config=config,
            desktop=desktop,
            webhook_url=webhook_url,
            state=state,
            state_file=state_file,
            announce=False,
        )
        errors += hook_errors
        if ok:
            synced += 1
            print(
                f"  {_paint('✓', '32', color)} {item.full_name} — synced ({detail})",
                file=output,
            )
        else:
            failed += 1
            errors += 1
            print(
                f"  {_paint('✗', '31', color)} {item.full_name} — {_short_error(detail)}",
                file=output,
            )
    print("", file=output)
    print(
        f"  {_paint('Result:', '1', color)} {synced} synced, {failed} failed.",
        file=output,
    )
    if errors and client.has_scope("workflow") is False:
        print(
            "  Hint: grant the workflow scope with "
            "`gh auth refresh -h github.com -s workflow`, then retry.",
            file=output,
        )
    return len(behind), synced, errors


def inspect_and_sync(
    client: GitHubClient,
    *,
    config: Dict[str, Any],
    do_sync: bool,
    dry_run: bool = False,
    desktop: bool = False,
    webhook_url: str = "",
    state_file: Optional[Path] = None,
) -> Tuple[int, int, int]:
    """Backward-compatible wrapper around sync_forks without a prompt."""
    return sync_forks(
        client,
        config=config,
        do_sync=do_sync,
        dry_run=dry_run,
        desktop=desktop,
        webhook_url=webhook_url,
        state_file=state_file,
    )


def run_github_watch(
    *,
    interval: int,
    do_sync: bool,
    dry_run: bool,
    config: Dict[str, Any],
    desktop: bool,
    webhook_url: str,
) -> int:
    if interval < 30:
        raise GitHubSyncError("--interval must be at least 30 seconds to avoid excessive GitHub API requests.")
    import time

    client = GitHubClient.from_gh_cli()
    login = client.get_account_login()
    print(f"Connected to GitHub as @{login}.")
    print(f"Watching fork repositories every {interval} seconds. Press Ctrl+C to stop.")
    try:
        while True:
            try:
                sync_forks(
                    client,
                    config=config,
                    do_sync=do_sync,
                    dry_run=dry_run,
                    desktop=desktop,
                    webhook_url=webhook_url,
                )
            except GitHubSyncError as exc:
                print(f"[repo-sync] scan error: {exc}", file=sys.stderr)
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped watching GitHub forks.")
        return 0
