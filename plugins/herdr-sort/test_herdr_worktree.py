import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import herdr_close
import herdr_worktree as wt
from test_herdr_cull import load_cli, row

SCRATCH = os.path.expanduser("~/scratch")


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, *args], check=True, capture_output=True,
                          text=True).stdout.strip()


class ScratchRepo(unittest.TestCase):
    def setUp(self):
        os.makedirs(SCRATCH, exist_ok=True)
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="herdr-wt-test-", dir=SCRATCH))
        self.addCleanup(shutil.rmtree, self.tmp)
        self.repo = os.path.join(self.tmp, "repo")
        self.links = os.path.join(self.tmp, "bin")
        self.save = os.path.join(self.tmp, "saved")
        os.makedirs(self.links)
        remote = os.path.join(self.tmp, "remote.git")
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", remote], check=True)
        subprocess.run(["git", "init", "-q", "-b", "main", self.repo], check=True)
        for key, value in (("user.name", "t"), ("user.email", "t@t"), ("commit.gpgsign", "false")):
            git(self.repo, "config", key, value)
        pathlib.Path(self.repo, ".gitignore").write_text("node_modules\n")
        pathlib.Path(self.repo, "a.txt").write_text("a\n")
        git(self.repo, "add", ".gitignore", "a.txt")
        git(self.repo, "commit", "-qm", "init")
        git(self.repo, "remote", "add", "origin", remote)
        git(self.repo, "push", "-q", "-u", "origin", "main")

    def worktree(self, name, branch, pushed=True):
        path = os.path.join(self.tmp, name)
        git(self.repo, "worktree", "add", "-q", "-b", branch, path, "main")
        pathlib.Path(path, name).write_text(name)
        git(path, "add", name)
        git(path, "commit", "-qm", name)
        if pushed:
            git(path, "push", "-q", "origin", branch)
        return path

    def check(self, path, other_cwds=()):
        return wt.check(path, other_cwds, link_dirs=(self.links,))

    def remove(self, path, other_cwds=()):
        return wt.remove(path, other_cwds, save_root=self.save, link_dirs=(self.links,))


class Check(ScratchRepo):
    def test_pushed_clean_worktree_passes(self):
        path = self.worktree("w", "feature")
        info = self.check(path)
        self.assertEqual("feature", info["branch"])
        self.assertEqual([], info["unreachable"])

    def test_refusals_name_their_reason(self):
        unpushed = self.worktree("u", "local", pushed=False)
        dirty = self.worktree("d", "dirty")
        pathlib.Path(dirty, "a.txt").write_text("edited\n")
        locked = self.worktree("l", "locked")
        git(self.repo, "worktree", "lock", locked)
        used = self.worktree("o", "other")
        linked = self.worktree("s", "linked")
        os.symlink(os.path.join(linked, "s"), os.path.join(self.links, "tool"))
        cases = {self.repo: "main checkout", unpushed: "not pushed or merged",
                 dirty: "uncommitted changes", locked: "locked",
                 os.path.join(used, "sub"): "not a directory", linked: "installed copy"}
        for path, reason in cases.items():
            with self.assertRaisesRegex(wt.Refused, reason, msg=path):
                self.check(path)
        with self.assertRaisesRegex(wt.Refused, "another open pane"):
            self.check(used, other_cwds=[os.path.join(used, ".")])


class Remove(ScratchRepo):
    def test_tags_reflog_only_commits_copies_untracked_and_keeps_the_branch(self):
        path = self.worktree("w", "feature", pushed=False)
        amended_away = git(path, "rev-parse", "HEAD")
        git(path, "commit", "-q", "--amend", "-m", "reworded")
        git(path, "push", "-q", "origin", "feature")
        pathlib.Path(path, "notes.txt").write_text("keep me")
        os.makedirs(os.path.join(path, "node_modules"))
        pathlib.Path(path, "node_modules", "big").write_text("x")

        outcome = self.remove(path)

        self.assertTrue(outcome["removed"], outcome)
        self.assertFalse(os.path.exists(path))
        self.assertNotIn(path, git(self.repo, "worktree", "list"))
        self.assertEqual([f"wt-removed/w/{amended_away[:12]}"], outcome["tags"])
        self.assertEqual(amended_away, git(self.repo, "rev-parse", outcome["tags"][0]))
        saved = outcome["untracked_saved"]
        self.assertEqual("keep me", pathlib.Path(saved, "notes.txt").read_text())
        self.assertFalse(os.path.exists(os.path.join(saved, "node_modules")))
        self.assertTrue(git(self.repo, "rev-parse", "--verify", "feature"))

    def test_removing_the_worktree_it_runs_in_steps_out_first(self):
        path = self.worktree("w", "feature")
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(path)
        self.assertTrue(self.remove(path)["removed"])
        self.assertFalse(os.path.exists(path))

    def test_refusal_leaves_the_worktree_and_says_why(self):
        path = self.worktree("w", "feature", pushed=False)
        outcome = self.remove(path)
        self.assertFalse(outcome["removed"])
        self.assertIn("not pushed or merged", outcome["reason"])
        self.assertTrue(os.path.isdir(path))


class FakeRecords:
    def load(self):
        return {}

    def close_fields(self, key, store):
        return {}


class FakeCull:
    def __init__(self, log):
        self.log = log

    def record(self, card, decision, linear, now):
        self.log.append("recorded")
        return [{"pane_id": "p1", "disposition": decision["disposition"]}]


class CloseRemovesWorktree(ScratchRepo):
    def setUp(self):
        super().setUp()
        self.cli = load_cli()
        self.events = []
        self.rows = [row("p1", "k1", focused=True), row("p2", "k2", cwd="/elsewhere")]
        card = {"id": "c1", "kind": "open", "title": "t", "workspace": "Growth",
                "members": [{"row": self.rows[0]}]}

        def fake_close(rows, index, extra=None, records=None):
            self.events.append(("closed", extra))
            return [{}]

        patches = [
            mock.patch.object(self.cli.ledger, "assess", return_value=(self.rows, {})),
            mock.patch.object(self.cli, "_assess", return_value=([card], None, None)),
            mock.patch.object(self.cli, "_cull",
                              return_value=(FakeCull(self.events), mock.Mock())),
            mock.patch.object(self.cli, "_records", return_value=(FakeRecords(),)),
            mock.patch.object(self.cli.ledger, "close", side_effect=fake_close),
            mock.patch.object(self.cli, "log"),
            mock.patch.object(wt, "LINK_DIRS", (self.links,)),
            mock.patch.object(wt, "SAVE_ROOT", self.save),
            mock.patch.dict(os.environ, {"HERDR_PANE_ID": "p1"}),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.assertIs(self.cli.herdr_worktree, wt)

    def close(self, path, **args):
        return self.cli.cmd_close(dict({"as": "done", "self": True, "yes": True,
                                        "remove_worktree": path}, **args), [])

    def test_refused_worktree_stops_the_close_before_anything_is_written(self):
        path = self.worktree("w", "feature", pushed=False)
        with self.assertRaisesRegex(SystemExit, "nothing written"):
            self.close(path)
        self.assertEqual([], self.events)
        self.assertTrue(os.path.isdir(path))

    def test_another_panes_cwd_inside_it_refuses(self):
        path = self.worktree("w", "feature")
        self.rows[1]["cwd"] = path
        with self.assertRaisesRegex(SystemExit, "another open pane"):
            self.close(path)
        self.assertEqual([], self.events)

    def test_approved_close_records_then_removes_then_closes(self):
        path = self.worktree("w", "feature")
        real = wt.remove

        def spy(*args, **kwargs):
            self.events.append("removed")
            return real(*args, **kwargs)

        with mock.patch.object(wt, "remove", side_effect=spy):
            self.close(path)
        self.assertEqual(["recorded", "removed"], self.events[:2])
        self.assertEqual("closed", self.events[2][0])
        outcome = self.events[2][1]["p1"]["worktree"]
        self.assertTrue(outcome["removed"], outcome)
        self.assertFalse(os.path.exists(path))

    def test_remove_worktree_needs_self(self):
        with self.assertRaisesRegex(SystemExit, "goes with --self"):
            self.cli.cmd_close({"as": "done", "remove_worktree": "/x"}, ["p2"])


class CardShowsRemoval(unittest.TestCase):
    def test_report_parses_the_worktree_from_the_command(self):
        text = ("**Verdict:** close as done\n\n    herdr-sort close --as done "
                "--remove-worktree /home/u/code/worktrees/x --yes --self\n\nRun it?")
        rep = herdr_close.report(text)
        self.assertEqual("/home/u/code/worktrees/x", rep["worktree"])
        self.assertTrue(herdr_close.clean_offer(rep))
        self.assertIsNone(herdr_close.report(text.replace(
            "--remove-worktree /home/u/code/worktrees/x ", ""))["worktree"])


if __name__ == "__main__":
    unittest.main()
