import unittest

from repo_sync.cli import _github_command_name, build_parser, describe_distance


class DescribeDistanceTests(unittest.TestCase):
    def test_up_to_date(self):
        self.assertEqual(describe_distance(0, 0), "Already up to date.")

    def test_remote_ahead(self):
        self.assertIn("behind by 3 commit(s)", describe_distance(0, 3))

    def test_local_ahead(self):
        self.assertIn("ahead by 2 commit(s)", describe_distance(2, 0))

    def test_diverged(self):
        message = describe_distance(2, 4)
        self.assertIn("diverged", message)
        self.assertIn("Manual reconciliation", message)


class CommandParserTests(unittest.TestCase):
    def test_no_command_defaults_to_ask(self):
        args = build_parser().parse_args([])
        self.assertIsNone(args.command)
        self.assertEqual(_github_command_name(args), "ask")

    def test_short_github_commands(self):
        self.assertEqual(build_parser().parse_args(["scan"]).command, "scan")
        self.assertEqual(build_parser().parse_args(["ask"]).command, "ask")
        self.assertEqual(build_parser().parse_args(["poll"]).command, "poll")
        self.assertTrue(build_parser().parse_args(["sync", "--dry-run"]).dry_run)

    def test_legacy_github_namespace_still_maps(self):
        self.assertEqual(
            _github_command_name(build_parser().parse_args(["github", "watch"])), "poll"
        )
        self.assertEqual(
            _github_command_name(build_parser().parse_args(["github", "init-config"])), "init"
        )
        legacy_sync = build_parser().parse_args(["github", "sync", "--dry-run"])
        self.assertEqual(_github_command_name(legacy_sync), "sync")
        self.assertTrue(legacy_sync.dry_run)

    def test_local_commands_are_unchanged(self):
        self.assertEqual(build_parser().parse_args(["check", "."]).command, "check")
        self.assertEqual(build_parser().parse_args(["update", "."]).command, "update")
        self.assertEqual(build_parser().parse_args(["watch", "."]).command, "watch")


if __name__ == "__main__":
    unittest.main()
