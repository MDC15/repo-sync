import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from repo_sync.github_sync import (
    ForkStatus,
    GitHubClient,
    GitHubSyncError,
    _next_link,
    format_fork_report,
    inspect_and_sync,
    prompt_yes_no,
    run_after_sync,
    sync_forks,
)


class FakeResponse:
    def __init__(self, body, headers):
        self._body = body
        self.headers = headers

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


class FakeGitHubClient(GitHubClient):
    def __init__(self, repositories, compare_response):
        super().__init__("fake-token")
        self.repositories = repositories
        self.compare_response = compare_response
        self.synced = []

    def get_account_login(self):
        return "dev"

    def get_all_repositories(self):
        return self.repositories

    def get_all_fork_repositories(self, login):
        self.asserted_login = login
        return self.repositories

    def get_repository_details(self, full_name):
        for repository in self.repositories:
            if repository.get("full_name") == full_name:
                return repository
        raise AssertionError(f"Unexpected repository lookup: {full_name}")

    def get(self, path):
        return self.compare_response

    def sync_fork(self, status):
        self.synced.append(status.full_name)
        return {"merge_type": "fast-forward"}


class GitHubSyncTests(unittest.TestCase):
    def setUp(self):
        self.fork = {
            "full_name": "dev/forked-lib",
            "fork": True,
            "archived": False,
            "default_branch": "main",
            "html_url": "https://github.com/dev/forked-lib",
            "permissions": {"push": True},
            "parent": {
                "full_name": "upstream/lib",
                "default_branch": "main",
                "owner": {"login": "upstream"},
            },
        }

    def test_scan_detects_upstream_ahead(self):
        client = FakeGitHubClient(
            [self.fork],
            {"ahead_by": 2, "behind_by": 0, "status": "ahead", "head_commit": {"sha": "abc123"}},
        )
        found = client.scan_forks()
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].update_available)
        self.assertEqual(found[0].ahead_by, 2)
        self.assertEqual(found[0].upstream_sha, "abc123")

    def test_scan_includes_read_only_forks_but_marks_them(self):
        non_fork = {"full_name": "dev/project", "fork": False}
        read_only = dict(self.fork, full_name="someone/fork", permissions={"push": False})
        client = FakeGitHubClient(
            [non_fork, read_only],
            {"ahead_by": 0, "behind_by": 0, "status": "identical"},
        )
        found = client.scan_forks()
        self.assertEqual([item.full_name for item in found], ["someone/fork"])
        self.assertFalse(found[0].writable)

    def test_fork_without_parent_metadata_is_reported_as_error(self):
        incomplete_fork = dict(self.fork)
        incomplete_fork.pop("parent")
        client = FakeGitHubClient([incomplete_fork], {})
        found = client.scan_forks()
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].status, "error")
        self.assertIn("did not return its parent metadata", found[0].error)

    def test_branch_name_mismatch_is_reported_not_guessed(self):
        fork = dict(self.fork)
        fork["default_branch"] = "master"
        client = FakeGitHubClient([fork], {})
        found = client.scan_forks()
        self.assertEqual(found[0].status, "branch-mismatch")

    def test_inspect_syncs_only_updated_forks(self):
        client = FakeGitHubClient(
            [self.fork],
            {"ahead_by": 1, "behind_by": 0, "status": "ahead", "head_commit": {"sha": "newsha"}},
        )
        with tempfile.TemporaryDirectory() as directory:
            state_file = Path(directory) / "state.json"
            with patch("repo_sync.github_sync.notify"):
                counts = inspect_and_sync(
                    client,
                    config={},
                    do_sync=True,
                    state_file=state_file,
                )
        self.assertEqual(counts, (1, 1, 0))
        self.assertEqual(client.synced, ["dev/forked-lib"])

    def test_read_only_fork_is_not_synced(self):
        read_only = dict(self.fork, permissions={"push": False})
        client = FakeGitHubClient(
            [read_only],
            {"ahead_by": 1, "behind_by": 0, "status": "ahead", "head_commit": {"sha": "newsha"}},
        )
        with tempfile.TemporaryDirectory() as directory:
            with patch("repo_sync.github_sync.notify"):
                counts = inspect_and_sync(
                    client, config={}, do_sync=True, state_file=Path(directory) / "state.json"
                )
        self.assertEqual(counts, (1, 0, 0))
        self.assertEqual(client.synced, [])

    def test_after_sync_placeholder_substitution(self):
        status = ForkStatus(
            "dev/fork", "origin/lib", "main", "main", 1, 0, "ahead",
            "https://github.com/dev/fork", "commit-sha",
        )
        config = {"after_sync": {"dev/fork": [["echo", "{repo}", "{upstream}", "{branch}"]]}}
        with patch("repo_sync.github_sync.subprocess.run") as run:
            run.return_value.returncode = 0
            self.assertEqual(run_after_sync(config, status), [0])
        command = run.call_args.args[0]
        self.assertEqual(command, ["echo", "dev/fork", "origin/lib", "main"])

    def test_get_all_repositories_follows_pagination(self):
        client = GitHubClient("fake-token")
        first = [{"full_name": "dev/one"}]
        second = [{"full_name": "dev/two"}]
        with patch.object(client, "request", side_effect=[
            (first, {"link": '<https://api.github.com/user/repos?page=2>; rel="next"'}),
            (second, {}),
        ]) as request:
            repos = client.get_all_repositories()
        self.assertEqual([repo["full_name"] for repo in repos], ["dev/one", "dev/two"])
        self.assertEqual(request.call_count, 2)

    def test_get_all_fork_repositories_uses_search_and_follows_pagination(self):
        client = GitHubClient("fake-token")
        first = {"items": [{"full_name": "dev/one"}], "total_count": 2}
        second = {"items": [{"full_name": "dev/two"}], "total_count": 2}
        with patch.object(client, "request", side_effect=[
            (first, {"link": '<https://api.github.com/search/repositories?q=user%3Adev%20fork%3Aonly&per_page=100&page=2>; rel="next"'}),
            (second, {}),
        ]) as request:
            repos = client.get_all_fork_repositories("dev")
        self.assertEqual([repo["full_name"] for repo in repos], ["dev/one", "dev/two"])
        self.assertIn("fork%3Aonly", request.call_args_list[0].args[1])
        self.assertEqual(request.call_count, 2)

    def test_archived_forks_are_discovered_but_not_writable(self):
        archived = dict(self.fork, archived=True)
        client = FakeGitHubClient(
            [archived],
            {"ahead_by": 0, "behind_by": 0, "status": "identical"},
        )
        found = client.scan_forks()
        self.assertEqual(len(found), 1)
        self.assertTrue(found[0].archived)
        self.assertFalse(found[0].writable)

    def test_next_link_parsing(self):
        header = '<https://api.github.com/user/repos?page=2>; rel="next", <https://api.github.com/user/repos?page=4>; rel="last"'
        self.assertEqual(_next_link(header), "https://api.github.com/user/repos?page=2")
        self.assertEqual(_next_link(' <https://api.github.com/user/repos?page=1>; rel="prev" '), "")

    def test_oauth_scopes_are_recorded_from_response_header(self):
        client = GitHubClient("fake-token")
        response = FakeResponse(b"{}", {"X-OAuth-Scopes": "repo, read:org, workflow"})
        with patch("urllib.request.urlopen", return_value=response):
            client.get("/user")
        self.assertTrue(client.has_scope("workflow"))
        self.assertFalse(client.has_scope("admin:org"))

    def test_unknown_scope_when_header_is_absent(self):
        client = GitHubClient("fake-token")
        self.assertIsNone(client.has_scope("workflow"))

    def test_workflow_scope_rejection_has_actionable_hint(self):
        client = GitHubClient("fake-token")
        body = json.dumps({
            "message": (
                "refusing to allow an OAuth App to create or update workflow "
                "`.github/workflows/ci.yml` without `workflow` scope"
            )
        }).encode("utf-8")
        error = urllib.error.HTTPError(
            "https://api.github.com/repos/dev/fork/merge-upstream",
            422,
            "Unprocessable Entity",
            {},
            io.BytesIO(body),
        )
        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(GitHubSyncError) as context:
                client.post("/repos/dev/fork/merge-upstream", {"branch": "main"})
        message = str(context.exception)
        self.assertIn("`workflow` scope", message)
        self.assertIn("gh auth refresh -h github.com -s workflow", message)

    def test_sync_warns_when_workflow_scope_is_missing(self):
        client = FakeGitHubClient(
            [self.fork],
            {"ahead_by": 1, "behind_by": 0, "status": "ahead", "head_commit": {"sha": "newsha"}},
        )
        client.scopes = {"repo"}
        with tempfile.TemporaryDirectory() as directory:
            stderr = io.StringIO()
            with patch("repo_sync.github_sync.notify"), patch("sys.stderr", stderr):
                inspect_and_sync(
                    client, config={}, do_sync=True, state_file=Path(directory) / "state.json"
                )
        self.assertIn("workflow", stderr.getvalue())
        self.assertIn("gh auth refresh", stderr.getvalue())

    def test_report_is_clean_and_lists_forks_behind(self):
        statuses = [
            ForkStatus("dev/fork", "up/lib", "main", "main", 2, 0, "ahead", "https://x"),
            ForkStatus("dev/ok", "up/ok", "main", "main", 0, 0, "identical", "https://y"),
        ]
        report = format_fork_report(statuses, login="dev", color=False)
        self.assertIn("GitHub forks — @dev", report)
        self.assertIn("1 fork(s) behind upstream", report)
        self.assertIn("dev/fork", report)
        self.assertIn("2 commit(s)", report)
        self.assertIn("1 up to date", report)
        self.assertNotIn("\033[", report)

    def test_prompt_yes_no_handles_answers(self):
        self.assertTrue(prompt_yes_no("?", input_fn=lambda _: "y"))
        self.assertTrue(prompt_yes_no("?", input_fn=lambda _: "YES"))
        self.assertTrue(prompt_yes_no("?", input_fn=lambda _: "c"))
        self.assertFalse(prompt_yes_no("?", input_fn=lambda _: "n"))
        self.assertFalse(prompt_yes_no("?", input_fn=lambda _: ""))

        def raise_eof(_):
            raise EOFError

        self.assertFalse(prompt_yes_no("?", input_fn=raise_eof))

    def test_ask_cancelled_does_not_sync(self):
        client = FakeGitHubClient(
            [self.fork],
            {"ahead_by": 1, "behind_by": 0, "status": "ahead", "head_commit": {"sha": "newsha"}},
        )
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch("repo_sync.github_sync.notify"):
                counts = sync_forks(
                    client,
                    config={},
                    do_sync=True,
                    confirm=True,
                    input_fn=lambda _: "n",
                    state_file=Path(directory) / "state.json",
                    stream=output,
                )
        self.assertEqual(counts, (1, 0, 0))
        self.assertEqual(client.synced, [])
        self.assertIn("Cancelled", output.getvalue())

    def test_ask_confirmed_syncs(self):
        client = FakeGitHubClient(
            [self.fork],
            {"ahead_by": 1, "behind_by": 0, "status": "ahead", "head_commit": {"sha": "newsha"}},
        )
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch("repo_sync.github_sync.notify"):
                counts = sync_forks(
                    client,
                    config={},
                    do_sync=True,
                    confirm=True,
                    input_fn=lambda _: "y",
                    state_file=Path(directory) / "state.json",
                    stream=output,
                )
        self.assertEqual(counts, (1, 1, 0))
        self.assertEqual(client.synced, ["dev/forked-lib"])
        self.assertIn("1 synced, 0 failed", output.getvalue())

    def test_failed_sync_is_reported_with_workflow_hint(self):
        class FailingClient(FakeGitHubClient):
            def sync_fork(self, status):
                raise GitHubSyncError(
                    "GitHub API returned HTTP 422: refusing to allow an OAuth App to "
                    "create or update workflow `.github/workflows/ci.yml` without "
                    "`workflow` scope."
                )

        client = FailingClient(
            [self.fork],
            {"ahead_by": 1, "behind_by": 0, "status": "ahead", "head_commit": {"sha": "newsha"}},
        )
        client.scopes = {"repo"}
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with patch("repo_sync.github_sync.notify"):
                counts = sync_forks(
                    client,
                    config={},
                    do_sync=True,
                    state_file=Path(directory) / "state.json",
                    stream=output,
                )
        text = output.getvalue()
        self.assertEqual(counts, (1, 0, 1))
        self.assertIn("missing `workflow` scope", text)
        self.assertIn("gh auth refresh -h github.com -s workflow", text)


if __name__ == "__main__":
    unittest.main()
