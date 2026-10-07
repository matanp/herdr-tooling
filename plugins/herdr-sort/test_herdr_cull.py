import os
import tempfile
import unittest
from unittest import mock

import herdr_cull as cull
import herdr_ledger as ledger

NOW = 2_000_000_000.0


def rec(key, **fields):
    base = {"key": key, "title": f"session {key}", "prompts": 3, "tool_calls": 5,
            "files": [], "prs": [], "size": 1000, "start": NOW - 9000, "last": NOW - 8000,
            "cwd": "/repo", "next_step": None, "resume": f"claude --resume {key}"}
    base.update(fields)
    return base


def row(pane_id, key, **fields):
    base = {"pane_id": pane_id, "tab_id": "t" + pane_id, "terminal_id": "term" + pane_id,
            "agent": "claude", "status": "idle", "title": f"tab {pane_id}", "cwd": "/repo",
            "workspace_id": "w1", "workspace": "Growth", "workspace_panes": 9,
            "tab_label": "", "focused": False, "key": key, "verdict": "CLOSE", "reason": ""}
    base.update(fields)
    return base


def close_event(key, closed_at, **fields):
    base = {"closed_at": closed_at, "key": key, "agent": "claude", "title": f"closed {key}",
            "workspace": "Growth", "cwd": "/repo", "resume": f"claude --resume {key}"}
    base.update(fields)
    return base


def note(**fields):
    base = {"disposition": "done", "outcome": "shipped it", "next_step": None,
            "open_items": [], "issues": [], "prs": [], "project": "Herdr", "draft_title": "Did X",
            "draft_body": "Draft body.", "model_session": "model-sid"}
    base.update(fields)
    return base


class FakeLLM:
    def __init__(self, notes=None, default=None, resolved=None, fail=False):
        self.notes = notes or {}
        self.default = default
        self.resolved = resolved or {}
        self.fail = fail
        self.calls = []
        self.resolve_calls = []

    def close_note(self, r):
        self.calls.append(r["key"])
        if self.fail:
            raise RuntimeError("model down")
        return self.notes.get(r["key"], self.default or note())

    def resolved_later(self, closed, candidates):
        self.resolve_calls.append((closed["rec"]["key"], [c["key"] for c in candidates]))
        hit = self.resolved.get(closed["rec"]["key"])
        return {"resolved_by": hit, "why": "same PR merged" if hit else "",
                "model_session": "resolve-sid"}


class FakeProbe:
    def __init__(self, dirty=(), prs=None, tickets=None, raise_on=()):
        self.dirty = set(dirty)
        self.prs = prs or {}
        self.tickets = tickets or {}
        self.raise_on = set(raise_on)

    def _maybe_raise(self, name):
        if name in self.raise_on:
            raise OSError(f"{name} broke")

    def repo_clean(self, path):
        self._maybe_raise("repo_clean")
        return path not in self.dirty

    def pr_state(self, n):
        self._maybe_raise("pr_state")
        return self.prs.get(str(n))

    def file_exists(self, path):
        return True

    def paths_match(self, a, b):
        return a == b

    def ticket_state(self, ident):
        return self.tickets.get(ident)

    def pane_alive(self, pane_id):
        return False

    def process_alive(self, name):
        return False


class FakeLinear:
    def __init__(self, fail=False):
        self.fail = fail
        self.calls = []

    def comment(self, ident, body):
        self.calls.append(("comment", ident, body))
        if self.fail:
            raise RuntimeError("linear down")
        return {"id": "c1", "issue": ident, "url": f"https://linear.app/{ident}#c1"}

    def create_issue(self, title, body, project, state):
        self.calls.append(("create_issue", title, body, project, state))
        if self.fail:
            raise RuntimeError("linear down")
        return {"id": "GROWTH-900", "url": "https://linear.app/GROWTH-900"}

    def issue(self, ident):
        raise AssertionError("record must not read issues")


def assess(rows=(), index=None, closes=(), llm=None, probe=None, notes=None):
    llm = llm or FakeLLM()
    cards = cull.assess(list(rows), index or {}, list(closes), llm, NOW, notes=notes,
                        probe=probe or FakeProbe())
    return cards, llm


def only(cards, kind):
    return [c for c in cards if kind == c["kind"]]


class TrivialRules(unittest.TestCase):
    def test_zero_prompt_session_is_trivial_without_llm(self):
        index = {"claude:a": rec("claude:a", prompts=0)}
        cards, llm = assess([row("p1", "claude:a")], index)
        self.assertEqual(1, len(cards))
        self.assertEqual("no prompts", cards[0]["trivial"])
        self.assertEqual("trivial", cards[0]["auto"])
        self.assertEqual([], llm.calls)

    def test_idle_shell_is_trivial_without_llm(self):
        cards, llm = assess([row("p1", None, agent=None, verdict="CLOSE")])
        self.assertEqual("idle shell", cards[0]["trivial"])
        self.assertEqual("trivial", cards[0]["auto"])
        self.assertEqual([], llm.calls)

    def test_no_tool_calls_files_or_prs_is_trivial(self):
        index = {"claude:a": rec("claude:a", tool_calls=0)}
        cards, llm = assess([row("p1", "claude:a")], index)
        self.assertTrue(cards[0]["trivial"])
        self.assertEqual([], llm.calls)

    def test_session_with_tool_calls_or_files_is_not_trivial(self):
        index = {"claude:a": rec("claude:a"),
                 "claude:b": rec("claude:b", tool_calls=0, files=["/repo/app/x.rb"])}
        cards, llm = assess([row("p1", "claude:a"), row("p2", "claude:b")], index)
        self.assertEqual([None, None], [c["trivial"] for c in cards])
        self.assertEqual({"claude:a", "claude:b"}, set(llm.calls))

    def test_busy_shell_is_not_trivial(self):
        cards, llm = assess([row("p1", None, agent=None, verdict="KEEP")])
        self.assertIsNone(cards[0]["trivial"])
        self.assertIsNone(cards[0]["auto"])
        self.assertEqual([], llm.calls)


class Grouping(unittest.TestCase):
    def test_two_tabs_sharing_a_pr_become_one_card(self):
        index = {"claude:a": rec("claude:a", prs=["54415"]),
                 "claude:b": rec("claude:b", prs=["54415", "54001"]),
                 "claude:c": rec("claude:c", prs=["11111"])}
        rows = [row("p1", "claude:a"), row("p2", "claude:b"), row("p3", "claude:c")]
        cards, _ = assess(rows, index)
        sizes = sorted(len(c["members"]) for c in cards)
        self.assertEqual([1, 2], sizes)
        pair = next(c for c in cards if 2 == len(c["members"]))
        self.assertEqual({"p1", "p2"}, {m["pane_id"] for m in pair["members"]})

    def test_tabs_sharing_an_issue_or_written_file_group(self):
        index = {"claude:a": rec("claude:a"), "claude:b": rec("claude:b"),
                 "claude:c": rec("claude:c", files=["/repo/app/x.rb"]),
                 "claude:d": rec("claude:d", files=["/repo/app/x.rb"])}
        llm = FakeLLM(notes={"claude:a": note(issues=["GROWTH-5"]),
                             "claude:b": note(issues=["GROWTH-5"])})
        rows = [row(f"p{k}", f"claude:{k}") for k in "abcd"]
        cards, _ = assess(rows, index, llm=llm)
        self.assertEqual([2, 2], sorted(len(c["members"]) for c in cards))


class CloseNotes(unittest.TestCase):
    def test_unchanged_transcript_makes_no_new_llm_call(self):
        index = {"claude:a": rec("claude:a", size=500)}
        notes = {}
        assess([row("p1", "claude:a")], index, notes=notes)
        cards, llm = assess([row("p1", "claude:a")], index, notes=notes)
        self.assertEqual([], llm.calls)
        self.assertEqual("done", cards[0]["suggested"])

    def test_grown_transcript_is_reanalysed(self):
        notes = {}
        assess([row("p1", "claude:a")], {"claude:a": rec("claude:a", size=500)}, notes=notes)
        _, llm = assess([row("p1", "claude:a")], {"claude:a": rec("claude:a", size=900)},
                        notes=notes)
        self.assertEqual(["claude:a"], llm.calls)
        self.assertEqual(900, notes["claude:a"]["size"])

    def test_llm_failure_yields_card_with_no_suggestion(self):
        cards, _ = assess([row("p1", "claude:a")], {"claude:a": rec("claude:a")},
                          llm=FakeLLM(fail=True))
        card = cards[0]
        self.assertIsNone(card["note"])
        self.assertIsNone(card["suggested"])
        self.assertIn("model down", card["note_error"])
        self.assertFalse(card["done_ok"])
        self.assertIsNone(card["auto"])

    def test_failed_note_is_not_cached(self):
        notes = {}
        assess([row("p1", "claude:a")], {"claude:a": rec("claude:a")}, llm=FakeLLM(fail=True),
               notes=notes)
        self.assertNotIn("claude:a", notes)


class OpenItems(unittest.TestCase):
    def _card(self, items, probe=None):
        llm = FakeLLM(default=note(open_items=items))
        cards, _ = assess([row("p1", "claude:a")], {"claude:a": rec("claude:a")}, llm=llm,
                          probe=probe or FakeProbe())
        return cards[0]

    def test_failing_repo_clean_blocks_one_key_done_and_lands_in_body(self):
        item = {"kind": "uncommitted", "text": "dash changes not committed",
                "check": {"type": "repo_clean", "path": "/repo"}}
        card = self._card([item], FakeProbe(dirty={"/repo"}))
        self.assertEqual("done", card["suggested"])
        self.assertFalse(card["done_ok"])
        self.assertIsNone(card["auto"])
        self.assertEqual("fail", card["open_items"][0]["result"])
        linear = FakeLinear()
        cull.record(card, {"disposition": "filed"}, linear, NOW)
        (call,) = linear.calls
        self.assertEqual("create_issue", call[0])
        self.assertIn("dash changes not committed", call[2])
        self.assertEqual("Backlog", call[4])

    def test_manual_item_does_not_block_done_but_is_carried_into_body(self):
        items = [{"kind": "action_left", "text": "kill the three orphan panes",
                  "check": {"type": "manual"}},
                 {"kind": "uncommitted", "text": "repo clean",
                  "check": {"type": "repo_clean", "path": "/repo"}}]
        card = self._card(items)
        self.assertEqual(["manual", "pass"], [i["result"] for i in card["open_items"]])
        self.assertTrue(card["done_ok"])
        self.assertEqual("done", card["auto"])
        linear = FakeLinear()
        cull.record(card, {"disposition": "done"}, linear, NOW)
        self.assertIn("kill the three orphan panes", linear.calls[0][2])
        # A passing item in the body would mean the filter is not applied at all.
        self.assertNotIn("- [pass]", linear.calls[0][2])

    def test_unknown_check_type_is_manual(self):
        item = {"kind": "unverified", "text": "run it", "check": {"type": "shell", "cmd": "rm"}}
        card = self._card([item])
        self.assertEqual("manual", card["open_items"][0]["result"])
        self.assertTrue(card["done_ok"])

    def test_probe_failure_is_manual_not_a_crash(self):
        item = {"kind": "uncommitted", "text": "x", "check": {"type": "repo_clean", "path": "/r"}}
        card = self._card([item], FakeProbe(raise_on={"repo_clean"}))
        self.assertEqual("manual", card["open_items"][0]["result"])

    def test_pr_merged_and_ticket_state_checks(self):
        items = [{"kind": "external_state", "text": "pr", "check": {"type": "pr_merged",
                                                                    "pr": "54415"}},
                 {"kind": "external_state", "text": "t",
                  "check": {"type": "ticket_state", "issue": "GROWTH-5", "state": "Done"}}]
        card = self._card(items, FakeProbe(prs={"54415": "open"},
                                           tickets={"GROWTH-5": "done"}))
        self.assertEqual(["fail", "pass"], [i["result"] for i in card["open_items"]])
        self.assertFalse(card["done_ok"])

    def test_artifacts_report_pr_doc_and_issue_state(self):
        index = {"claude:a": rec("claude:a", prs=["54415"], files=["/repo/docs/plan.md"])}
        llm = FakeLLM(default=note(issues=["GROWTH-5"]))
        cards, _ = assess([row("p1", "claude:a")], index, llm=llm,
                          probe=FakeProbe(prs={"54415": "merged"}, tickets={"GROWTH-5": "Done"}))
        arts = cards[0]["artifacts"]
        self.assertEqual([{"pr": "54415", "state": "merged"}], arts["prs"])
        self.assertEqual([{"path": "/repo/docs/plan.md", "exists": True}], arts["docs"])
        self.assertEqual([{"id": "GROWTH-5", "state": "Done"}], arts["issues"])


class Record(unittest.TestCase):
    def _card(self, **note_fields):
        llm = FakeLLM(default=note(**note_fields))
        cards, _ = assess([row("p1", "claude:a")], {"claude:a": rec("claude:a", prs=["54415"])},
                          llm=llm)
        return cards[0]

    def test_referenced_existing_issue_gets_a_comment_not_a_new_issue(self):
        card = self._card(issues=["GROWTH-77"])
        linear = FakeLinear()
        (fields,) = cull.record(card, {"disposition": "done"}, linear, NOW)
        self.assertEqual(["comment"], [c[0] for c in linear.calls])
        self.assertEqual("GROWTH-77", linear.calls[0][1])
        self.assertIn("claude --resume claude:a", linear.calls[0][2])
        self.assertIn("#54415", linear.calls[0][2])
        self.assertEqual("GROWTH-77#c1", fields["ref"])
        self.assertEqual("https://linear.app/GROWTH-77#c1", fields["ref_url"])
        self.assertEqual("model-sid", fields["model_session"])
        self.assertEqual("p1", fields["pane_id"])
        self.assertNotIn("closed_at", fields)

    def test_decision_ref_overrides_note_issue(self):
        card = self._card(issues=["GROWTH-77"])
        linear = FakeLinear()
        cull.record(card, {"disposition": "filed", "ref": "GROWTH-1"}, linear, NOW)
        self.assertEqual("GROWTH-1", linear.calls[0][1])

    def test_done_without_reference_creates_done_issue_in_suggested_project(self):
        card = self._card(project="Herdr")
        linear = FakeLinear()
        (fields,) = cull.record(card, {"disposition": "done"}, linear, NOW)
        (call,) = linear.calls
        self.assertEqual(("create_issue", "Did X"), call[:2])
        self.assertEqual(("Herdr", "Done"), call[3:])
        self.assertEqual("GROWTH-900", fields["ref"])
        self.assertEqual("done", fields["disposition"])
        self.assertEqual("shipped it", fields["note"])

    def test_done_without_project_falls_back_to_session_log(self):
        card = self._card(project=None)
        linear = FakeLinear()
        cull.record(card, {"disposition": "done"}, linear, NOW)
        self.assertEqual("Agent session log", linear.calls[0][3])

    def test_decision_project_title_and_body_win(self):
        card = self._card(project="Herdr")
        linear = FakeLinear()
        cull.record(card, {"disposition": "filed", "project": "Mentions", "title": "T",
                           "body": "edited"}, linear, NOW)
        self.assertEqual(("create_issue", "T", "edited", "Mentions", "Backlog"), linear.calls[0])

    def test_dropped_without_reason_is_rejected(self):
        linear = FakeLinear()
        with self.assertRaises(ValueError):
            cull.record(self._card(), {"disposition": "dropped", "note": "  "}, linear, NOW)
        self.assertEqual([], linear.calls)

    def test_dropped_with_reason_records_without_linear(self):
        linear = FakeLinear()
        (fields,) = cull.record(self._card(), {"disposition": "dropped", "note": "superseded"},
                                linear, NOW)
        self.assertEqual(("dropped", "superseded"), (fields["disposition"], fields["note"]))
        self.assertEqual([], linear.calls)

    def test_linear_failure_records_nothing(self):
        for card in (self._card(), self._card(issues=["GROWTH-77"])):
            with self.assertRaises(RuntimeError):
                cull.record(card, {"disposition": "done"}, FakeLinear(fail=True), NOW)

    def test_kept_and_trivial_make_no_linear_call(self):
        linear = FakeLinear()
        self.assertEqual([], cull.record(self._card(), {"disposition": "kept"}, linear, NOW))
        (fields,) = cull.record(self._card(), {"disposition": "trivial"}, linear, NOW)
        self.assertEqual("trivial", fields["disposition"])
        self.assertEqual([], linear.calls)

    def test_merged_comments_on_the_surviving_ticket(self):
        linear = FakeLinear()
        with self.assertRaises(ValueError):
            cull.record(self._card(issues=["GROWTH-77"]), {"disposition": "merged"}, linear, NOW)
        self.assertEqual([], linear.calls)
        (fields,) = cull.record(self._card(issues=["GROWTH-77"]),
                                {"disposition": "merged", "ref": "GROWTH-3"}, linear, NOW)
        self.assertEqual(("comment", "GROWTH-3"), linear.calls[0][:2])
        self.assertEqual("GROWTH-3#c1", fields["ref"])

    def test_one_close_fields_per_group_member(self):
        index = {"claude:a": rec("claude:a", prs=["54415"]),
                 "claude:b": rec("claude:b", prs=["54415"])}
        cards, _ = assess([row("p1", "claude:a"), row("p2", "claude:b")], index)
        linear = FakeLinear()
        out = cull.record(cards[0], {"disposition": "done"}, linear, NOW)
        self.assertEqual({"p1", "p2"}, {f["pane_id"] for f in out})
        self.assertEqual(1, len(linear.calls))
        self.assertIn("claude --resume claude:a", linear.calls[0][2])
        self.assertIn("claude --resume claude:b", linear.calls[0][2])

    def test_unknown_disposition_is_rejected(self):
        with self.assertRaises(ValueError):
            cull.record(self._card(), {"disposition": "shelved"}, FakeLinear(), NOW)


class Loops(unittest.TestCase):
    def test_pre_disposition_close_with_next_step_is_a_loop(self):
        index = {"claude:a": rec("claude:a", next_step="Next step: run the backfill"),
                 "claude:b": rec("claude:b")}
        llm = FakeLLM(notes={"claude:a": note(next_step=None),
                             "claude:b": note(next_step=None)})
        closes = [close_event("claude:a", NOW - 5000), close_event("claude:b", NOW - 4000)]
        cards, _ = assess([], index, closes, llm=llm)
        loops = only(cards, "loop")
        self.assertEqual(["claude:a"], [c["members"][0]["key"] for c in loops])
        self.assertEqual(NOW - 5000, loops[0]["members"][0]["event"]["closed_at"])

    def test_note_next_step_alone_makes_a_loop(self):
        index = {"claude:a": rec("claude:a")}
        llm = FakeLLM(default=note(next_step="file the follow-up"))
        cards, _ = assess([], index, [close_event("claude:a", NOW - 5000)], llm=llm)
        self.assertEqual(1, len(only(cards, "loop")))

    def test_loop_with_failed_note_is_still_shown(self):
        index = {"claude:a": rec("claude:a")}
        cards, _ = assess([], index, [close_event("claude:a", NOW - 5000)],
                          llm=FakeLLM(fail=True))
        (loop,) = only(cards, "loop")
        self.assertIsNotNone(loop["note_error"])

    def test_closed_with_disposition_is_not_a_loop(self):
        index = {"claude:a": rec("claude:a", next_step="Next step: x")}
        closes = [close_event("claude:a", NOW - 5000, disposition="filed", ref="GROWTH-1")]
        cards, _ = assess([], index, closes)
        self.assertEqual([], only(cards, "loop"))

    def test_trivial_closes_never_appear_as_loops(self):
        index = {"claude:a": rec("claude:a", prompts=0, next_step="Next step: x"),
                 "claude:b": rec("claude:b", next_step="Next step: y")}
        closes = [close_event("claude:a", NOW - 5000),
                  close_event("claude:b", NOW - 4000, disposition="trivial"),
                  close_event(None, NOW - 3000, agent=None)]
        cards, llm = assess([], index, closes)
        self.assertEqual([], only(cards, "loop"))
        self.assertEqual([], llm.calls)

    def test_session_reopened_into_an_open_tab_is_not_a_loop(self):
        index = {"claude:a": rec("claude:a", next_step="Next step: x")}
        cards, _ = assess([row("p1", "claude:a")], index, [close_event("claude:a", NOW - 5000)])
        self.assertEqual([], only(cards, "loop"))
        self.assertEqual(1, len(only(cards, "open")))

    def test_loop_resolved_by_later_session_is_marked_with_it(self):
        closed_at = NOW - 5000
        index = {"claude:a": rec("claude:a", prs=["54415"], next_step="Next step: merge it"),
                 "claude:later": rec("claude:later", prs=["54415"], start=closed_at + 100,
                                     cwd="/elsewhere"),
                 "claude:before": rec("claude:before", prs=["54415"], start=closed_at - 100),
                 "claude:unrelated": rec("claude:unrelated", start=closed_at + 100,
                                         cwd="/elsewhere")}
        llm = FakeLLM(default=note(next_step=None), resolved={"claude:a": "claude:later"})
        cards, _ = assess([], index, [close_event("claude:a", closed_at)], llm=llm)
        (loop,) = only(cards, "loop")
        self.assertEqual("claude:later", loop["resolved_by"]["key"])
        self.assertEqual("resolve-sid", loop["resolved_by"]["model_session"])
        self.assertIsNone(loop["auto"])
        self.assertEqual([("claude:a", ["claude:later"])], llm.resolve_calls)

    def test_unresolved_loop_and_resolution_naming_a_non_candidate(self):
        closed_at = NOW - 5000
        index = {"claude:a": rec("claude:a", next_step="Next step: x"),
                 "claude:later": rec("claude:later", start=closed_at + 100)}
        llm = FakeLLM(default=note(next_step=None), resolved={"claude:a": "claude:ghost"})
        cards, _ = assess([], index, [close_event("claude:a", closed_at)], llm=llm)
        self.assertIsNone(only(cards, "loop")[0]["resolved_by"])

    def test_resolved_later_cached_until_candidates_change(self):
        closed_at = NOW - 5000
        index = {"claude:a": rec("claude:a", next_step="Next step: x"),
                 "claude:later": rec("claude:later", start=closed_at + 100)}
        notes = {}
        closes = [close_event("claude:a", closed_at)]
        assess([], index, closes, notes=notes)
        _, llm = assess([], index, closes, notes=notes)
        self.assertEqual([], llm.resolve_calls)
        index["claude:later"]["size"] = 5000
        _, llm = assess([], index, closes, notes=notes)
        self.assertEqual(1, len(llm.resolve_calls))

    def test_loop_close_fields_carry_closed_at(self):
        index = {"claude:a": rec("claude:a", next_step="Next step: x")}
        cards, _ = assess([], index, [close_event("claude:a", NOW - 5000)])
        (fields,) = cull.record(only(cards, "loop")[0],
                                {"disposition": "dropped", "note": "obsolete"}, FakeLinear(), NOW)
        self.assertEqual(NOW - 5000, fields["closed_at"])
        self.assertEqual("claude:a", fields["key"])
        self.assertIsNone(fields["pane_id"])


class LedgerScan(unittest.TestCase):
    def test_model_run_project_folder_is_excluded(self):
        with tempfile.TemporaryDirectory() as projects:
            run_dir = os.path.join(projects, "x", "cull-runs")
            paths = {}
            for name, cwd in (("user", "/home/u/code/app"), ("model", run_dir),
                              ("summary", os.path.join(projects, "x", "summarize"))):
                with mock.patch.object(ledger, "CLAUDE_PROJECTS", projects):
                    folder = ledger.claude_project_dir(cwd)
                os.makedirs(folder)
                paths[name] = os.path.join(folder, "s.jsonl")
                open(paths[name], "w").close()
            with mock.patch.multiple(
                    ledger, CLAUDE_PROJECTS=projects, CODEX_GLOBS=[],
                    CLAUDE_GLOB=os.path.join(projects, "*", "*.jsonl"),
                    MODEL_CWD=run_dir, SUMMARY_CWD=os.path.join(projects, "x", "summarize")):
                found = [path for _, path in ledger._sources()]
        self.assertEqual([paths["user"]], found)


if __name__ == "__main__":
    unittest.main()
