import json
import os
import tempfile
import unittest
from unittest import mock

import herdr_records as records

NOW = 2_000_000_000.0


class FakeLLM:
    def __init__(self, fail=False, fail_audit=False, during=None):
        self.fail, self.fail_audit, self.during = fail, fail_audit, during
        self.calls = []

    def recap(self, rec, previous=None):
        self.calls.append(("recap", rec["key"]))
        if self.during:
            self.during()
        if self.fail:
            raise RuntimeError("model down")
        return {"recap": f"recap of {rec['key']} at {rec['size']}", "needs": "an answer",
                "next_step": "merge it", "model": "fake-haiku"}

    def close_note(self, rec):
        self.calls.append(("audit", rec["key"]))
        if self.fail_audit:
            raise RuntimeError("model down")
        return {"disposition": "filed", "outcome": "did it", "open_items": [],
                "draft_title": "T", "draft_body": "B"}


class Refresh(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(records, "INFLIGHT_DIR",
                                    os.path.join(self.tmp.name, "inflight"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.index = {}

    def session(self, key, size=20000, quiet=600, agent="claude", folder="proj", **fields):
        """A fixture transcript of `size` bytes, last written `quiet` seconds before NOW."""
        path = os.path.join(self.tmp.name, folder, key.split(":")[1] + ".jsonl")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("x" * size)
        os.utime(path, (NOW - quiet, NOW - quiet))
        rec = {"key": key, "agent": agent, "file": path, "size": size, "prompts": 3,
               "title": f"session {key}", "first_ask": "build the thing"}
        rec.update(fields)
        self.index[key] = rec
        return rec

    def grow(self, key, by, quiet=600):
        path = self.index[key]["file"]
        with open(path, "a") as handle:
            handle.write("y" * by)
        os.utime(path, (NOW - quiet, NOW - quiet))
        self.index[key]["size"] = os.path.getsize(path)

    def row(self, key, status="idle", agent="claude", **fields):
        base = {"pane_id": "p-" + (key or "shell"), "key": key, "agent": agent,
                "status": status, "title": "tab"}
        base.update(fields)
        return base

    def test_grown_settled_session_gets_a_record(self):
        self.session("claude:a")
        llm = FakeLLM()
        out = records.refresh([self.row("claude:a")], self.index, {}, llm, NOW)
        self.assertEqual([("recap", "claude:a")], llm.calls)
        rec = out["claude:a"]
        self.assertEqual("recap of claude:a at 20000", rec["recap"])
        self.assertEqual("an answer", rec["needs"])
        self.assertEqual("merge it", rec["next_step"])
        self.assertEqual(20000, rec["built_for"])
        self.assertEqual(NOW, rec["built_at"])
        self.assertEqual("fake-haiku", rec["model"])

    def test_working_or_just_settled_agent_is_skipped(self):
        self.session("claude:a")
        self.session("claude:b", quiet=10)
        self.session("claude:c")
        llm = FakeLLM()
        out = records.refresh([self.row("claude:a", status="working"), self.row("claude:b"),
                               self.row("claude:c", status="blocked")],
                              self.index, {}, llm, NOW)
        self.assertEqual([], llm.calls)
        self.assertNotIn("recap", out["claude:a"])

    def test_unchanged_or_barely_grown_transcript_makes_no_call(self):
        self.session("claude:a")
        llm = FakeLLM()
        first = records.refresh([self.row("claude:a")], self.index, {}, llm, NOW)
        records.refresh([self.row("claude:a")], self.index, first, llm, NOW + 60)
        self.grow("claude:a", records.MIN_GROWTH - 1)
        records.refresh([self.row("claude:a")], self.index, first, llm, NOW + 120)
        self.assertEqual(1, len(llm.calls))
        self.grow("claude:a", 1)
        again = records.refresh([self.row("claude:a")], self.index, first, llm, NOW + 180)
        self.assertEqual(2, len(llm.calls))
        self.assertEqual(20000 + records.MIN_GROWTH, again["claude:a"]["built_for"])

    def test_force_refreshes_a_barely_grown_unsettled_session_but_not_an_unchanged_one(self):
        self.session("claude:a")
        llm = FakeLLM()
        first = records.refresh([self.row("claude:a")], self.index, {}, llm, NOW)
        records.refresh([self.row("claude:a")], self.index, first, llm, NOW, force=True)
        self.grow("claude:a", 10, quiet=5)
        records.refresh([self.row("claude:a")], self.index, first, llm, NOW, force=True)
        self.assertEqual(2, len(llm.calls))

    def test_model_failure_keeps_the_previous_record(self):
        self.session("claude:a")
        first = records.refresh([self.row("claude:a")], self.index, {}, FakeLLM(), NOW)
        self.grow("claude:a", records.MIN_GROWTH)
        out = records.refresh([self.row("claude:a")], self.index, first, FakeLLM(fail=True),
                              NOW + 600)
        rec = out["claude:a"]
        self.assertEqual("recap of claude:a at 20000", rec["recap"])
        self.assertEqual(20000, rec["built_for"])
        self.assertIn("model down", rec["error"])

    def test_failed_size_is_not_retried_until_the_backoff_passes(self):
        self.session("claude:a")
        llm = FakeLLM(fail=True)
        out = records.refresh([self.row("claude:a")], self.index, {}, llm, NOW)
        out = records.refresh([self.row("claude:a")], self.index, out, llm, NOW + 60)
        self.assertEqual(1, len(llm.calls))
        out = records.refresh([self.row("claude:a")], self.index, out, FakeLLM(),
                              NOW + records.FAIL_RETRY_S)
        self.assertNotIn("error", out["claude:a"])
        self.assertIn("recap", out["claude:a"])

    def test_session_whose_tab_is_gone_is_frozen_not_refreshed(self):
        self.session("claude:a")
        first = records.refresh([self.row("claude:a")], self.index, {}, FakeLLM(), NOW)
        self.grow("claude:a", records.MIN_GROWTH * 2)
        llm = FakeLLM()
        out = records.refresh([], self.index, first, llm, NOW + 600)
        self.assertEqual([], llm.calls)
        self.assertEqual(NOW + 600, out["claude:a"]["frozen_at"])
        self.assertEqual(first["claude:a"]["recap"], out["claude:a"]["recap"])
        later = records.refresh([], self.index, out, llm, NOW + 1200)
        self.assertEqual(NOW + 600, later["claude:a"]["frozen_at"])
        reopened = records.refresh([self.row("claude:a")], self.index, later, llm, NOW + 1800)
        self.assertNotIn("frozen_at", reopened["claude:a"])

    def test_audit_runs_only_when_asked_or_after_long_idleness(self):
        self.session("claude:a")
        self.session("claude:b", quiet=records.AUDIT_IDLE_S + 1)
        self.session("claude:c", quiet=5)
        llm = FakeLLM()
        out = records.refresh([self.row(k) for k in ("claude:a", "claude:b", "claude:c")],
                              self.index, {}, llm, NOW, audit={"claude:c"})
        audits = sorted(k for kind, k in llm.calls if "audit" == kind)
        self.assertEqual(["claude:b", "claude:c"], audits)
        self.assertNotIn("audit", out["claude:a"])
        self.assertEqual("filed", out["claude:c"]["audit"]["disposition"])
        self.assertEqual(20000, out["claude:c"]["audit_for"])
        self.assertIn("recap", out["claude:c"])

    def test_a_fresh_audit_is_reused(self):
        self.session("claude:a")
        llm = FakeLLM()
        out = records.refresh([self.row("claude:a")], self.index, {}, llm, NOW,
                              audit={"claude:a"})
        records.refresh([self.row("claude:a")], self.index, out, llm, NOW + 60,
                        audit={"claude:a"})
        self.assertEqual(1, sum("audit" == kind for kind, _ in llm.calls))

    def test_audit_failure_keeps_the_recap_it_made(self):
        self.session("claude:a")
        out = records.refresh([self.row("claude:a")], self.index, {},
                              FakeLLM(fail_audit=True), NOW, audit={"claude:a"})
        self.assertIn("recap", out["claude:a"])
        self.assertNotIn("audit", out["claude:a"])
        self.assertIn("audit:", out["claude:a"]["error"])

    def test_shells_model_runs_and_unindexed_sessions_are_excluded(self):
        own = self.session("claude:own")
        with mock.patch("herdr_ledger.claude_project_dir",
                        side_effect=lambda cwd: os.path.dirname(own["file"])):
            llm = FakeLLM()
            out = records.refresh([self.row(None, agent=None), self.row("claude:own"),
                                   self.row("claude:unknown")], self.index, {}, llm, NOW)
        self.assertEqual([], llm.calls)
        self.assertEqual({}, out)

    def test_codex_sessions_are_recapped(self):
        self.session("codex:c1", agent="codex")
        llm = FakeLLM()
        out = records.refresh([self.row("codex:c1", agent="codex")], self.index, {}, llm, NOW)
        self.assertEqual([("recap", "codex:c1")], llm.calls)
        self.assertIn("recap", out["codex:c1"])

    def test_second_refresh_while_one_is_in_flight_is_a_no_op(self):
        self.session("claude:a")
        inner = FakeLLM()
        nested = {}

        def during():
            nested["out"] = records.refresh([self.row("claude:a")], self.index, {}, inner, NOW)

        outer = FakeLLM(during=during)
        out = records.refresh([self.row("claude:a")], self.index, {}, outer, NOW)
        self.assertEqual([], inner.calls)
        self.assertNotIn("recap", nested["out"]["claude:a"])
        self.assertIn("recap", out["claude:a"])


class Store(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.object(records, "RECORDS_PATH",
                                    os.path.join(self.tmp.name, "records.json"))
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch("herdr_ledger.STATE_DIR", self.tmp.name)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_save_keeps_the_newer_tier_from_each_writer(self):
        records.save({"k": {"key": "k", "recap": "new", "built_at": 20, "audit": {"a": 1},
                            "audit_at": 5}})
        merged = records.save({"k": {"key": "k", "recap": "old", "built_at": 10,
                                     "audit": {"a": 2}, "audit_at": 30}})
        self.assertEqual("new", merged["k"]["recap"])
        self.assertEqual({"a": 2}, merged["k"]["audit"])

    def test_success_clears_an_error_another_writer_saved(self):
        records.save({"k": {"key": "k", "error": "boom", "failed_for": 1, "failed_at": 10}})
        merged = records.save({"k": {"key": "k", "recap": "fine", "built_at": 20}})
        self.assertNotIn("error", merged["k"])

    def test_long_frozen_records_are_pruned(self):
        merged = records.save({"old": {"key": "old", "frozen_at": NOW - records.PRUNE_S},
                               "new": {"key": "new", "frozen_at": NOW - 60}}, now=NOW)
        self.assertEqual(["new"], list(merged))
        with open(records.RECORDS_PATH) as handle:
            self.assertEqual(["new"], list(json.load(handle)))


class Readers(unittest.TestCase):
    def test_view_prefers_the_record_then_falls_back(self):
        store = {"k": {"recap": "R", "needs": "N", "built_at": 5}}
        self.assertEqual(("R", "record", 5),
                         tuple(records.view("k", store)[f] for f in ("text", "source", "at")))
        fb = records.view("other", store, {"first_ask": "ask", "recap": "away"})
        self.assertEqual(("ask · away", "fallback"), (fb["text"], fb["source"]))
        self.assertIsNone(records.view("other", store, {}))

    def test_closed_view_reads_the_frozen_record_before_the_old_summary(self):
        rec = {"summary": "haiku summary"}
        self.assertEqual(("R", "record"), records.closed_view({"record": {"recap": "R"}}, rec))
        self.assertEqual(("haiku summary", "haiku"), records.closed_view({}, rec))


if __name__ == "__main__":
    unittest.main()
