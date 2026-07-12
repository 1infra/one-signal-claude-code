#!/usr/bin/env python3
"""Unit tests for one_signal_hook.py's skill/MCP attribution.

No network calls. Run with:

    uv run python plugins/one-signal/test_hook.py

From the repo root, `pnpm test:plugins` runs this suite and the
one-signal-codex one together.
"""
import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest import mock


HOOK_PATH = Path(__file__).resolve().parent / "hooks" / "one_signal_hook.py"
SPEC = importlib.util.spec_from_file_location("one_signal_hook", HOOK_PATH)
assert SPEC and SPEC.loader
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


def make_turn(assistant_content):
    """A minimal one-round Turn with a single assistant message."""
    return hook.Turn(
        user_msg={"timestamp": "2026-07-12T00:00:00Z", "message": {"content": "do it"}},
        assistant_msgs=[{
            "timestamp": "2026-07-12T00:00:01Z",
            "message": {"id": "msg-1", "content": assistant_content},
        }],
        tool_results_by_id={},
        injected_by_tool_id={},
        end_offset=1,
    )


class TestAttribution(unittest.TestCase):
    def setUp(self):
        # Pin the skill-tagging flag so the assertions don't depend on the
        # developer's ambient CC_ONE_SIGNAL_SKILL_TAGS / CLAUDE_PLUGIN_OPTION_*
        # environment.
        self._skill_tags = hook.SKILL_TAGS
        hook.SKILL_TAGS = True

    def tearDown(self):
        hook.SKILL_TAGS = self._skill_tags

    def _trace(self, turn):
        events = hook.build_turn_events("session-1", 1, turn, Path("transcript.jsonl"))
        return next(e for e in events if e["type"] == "trace-create")["body"]

    def test_real_skill_input_shape_is_attributed(self):
        # The real Claude Code Skill tool input uses the `skill` key.
        turn = make_turn([
            {"type": "tool_use", "id": "skill-1", "name": "Skill",
             "input": {"skill": "code-review", "args": "review this"}},
        ])
        trace = self._trace(turn)
        self.assertEqual(trace["metadata"]["skill_names"], ["code-review"])
        self.assertIn("skill:code-review", trace["tags"])

    def test_first_turn_uploads_global_and_project_instruction_documents(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            project = home / "work" / "project"
            nested = project / "packages" / "app"
            (project / ".git").mkdir(parents=True)
            nested.mkdir(parents=True)
            (home / ".codex").mkdir()
            (home / ".claude").mkdir()
            (home / ".codex" / "AGENTS.md").write_text("global agents", encoding="utf-8")
            (home / ".claude" / "CLAUDE.md").write_text("global claude", encoding="utf-8")
            (project / "AGENTS.md").write_text("project agents", encoding="utf-8")
            (nested / "CLAUDE.md").write_text("nested claude", encoding="utf-8")
            turn = make_turn("done")
            turn.user_msg["cwd"] = str(nested)

            with mock.patch.object(hook.Path, "home", return_value=home):
                trace = self._trace(turn)

            documents = trace["metadata"]["instruction_documents"]
            self.assertEqual([document["path"] for document in documents], [
                "~/.codex/AGENTS.md",
                "~/.claude/CLAUDE.md",
                "AGENTS.md",
                "packages/app/CLAUDE.md",
            ])

            later = hook.build_turn_events("session-1", 2, turn, Path("transcript.jsonl"))
            later_trace = next(event for event in later if event["type"] == "trace-create")["body"]
            self.assertNotIn("instruction_documents", later_trace["metadata"])

    def test_mcp_tool_is_attributed_on_span_and_tag(self):
        turn = make_turn([
            {"type": "tool_use", "id": "mcp-1", "name": "mcp__github__get_pull_request",
             "input": {"number": 42}},
        ])
        turn.tool_results_by_id = {"mcp-1": {"content": "pull request 42", "timestamp": "2026-07-12T00:00:02Z"}}
        events = hook.build_turn_events("session-1", 1, turn, Path("transcript.jsonl"))
        trace = next(e for e in events if e["type"] == "trace-create")["body"]
        mcp = next(
            e["body"] for e in events
            if (e["body"].get("metadata") or {}).get("mcp_server") == "github"
        )
        self.assertIn("mcp:github:get_pull_request", trace["tags"])
        self.assertEqual(mcp["metadata"]["mcp_tool"], "get_pull_request")
        self.assertEqual(mcp["metadata"]["result_status"], "success")

    def test_tool_result_error_status_is_emitted_on_tool_observation(self):
        rows = [
            ({"type": "user", "timestamp": "2026-07-12T00:00:00Z", "message": {"content": "run it"}}, 1),
            ({
                "type": "assistant",
                "timestamp": "2026-07-12T00:00:01Z",
                "message": {
                    "id": "msg-1",
                    "content": [{"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {}}],
                },
            }, 2),
            ({
                "type": "user",
                "timestamp": "2026-07-12T00:00:02Z",
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": "tool-1",
                    "content": "failed",
                    "is_error": True,
                }]},
            }, 3),
        ]

        turn = hook.build_turns(rows)[0]
        events = hook.build_turn_events("session-1", 1, turn, Path("transcript.jsonl"))
        tool = next(
            event["body"] for event in events
            if (event["body"].get("metadata") or {}).get("tool_name") == "Bash"
        )

        self.assertEqual(tool["metadata"]["result_status"], "error")

    def test_skill_tags_flag_gates_both_tags_and_metadata(self):
        hook.SKILL_TAGS = False
        turn = make_turn([
            {"type": "tool_use", "id": "skill-1", "name": "Skill", "input": {"skill": "code-review"}},
        ])
        trace = self._trace(turn)
        self.assertNotIn("skill_names", trace["metadata"])
        self.assertNotIn("skill:code-review", trace["tags"])

    def test_duplicate_invocations_are_deduped(self):
        turn = make_turn([
            {"type": "tool_use", "id": "s1", "name": "Skill", "input": {"skill": "code-review"}},
            {"type": "tool_use", "id": "s2", "name": "Skill", "input": {"skill": "code-review"}},
            {"type": "tool_use", "id": "m1", "name": "mcp__github__get_pr", "input": {}},
            {"type": "tool_use", "id": "m2", "name": "mcp__github__get_pr", "input": {}},
        ])
        trace = self._trace(turn)
        self.assertEqual(trace["metadata"]["skill_names"], ["code-review"])
        self.assertEqual([t for t in trace["tags"] if t.startswith("mcp:")], ["mcp:github:get_pr"])

    def test_non_mcp_and_malformed_names_produce_no_mcp_tag(self):
        turn = make_turn([
            {"type": "tool_use", "id": "b1", "name": "Bash", "input": {"command": "ls"}},
            {"type": "tool_use", "id": "x1", "name": "mcp__github", "input": {}},
        ])
        trace = self._trace(turn)
        self.assertEqual([t for t in trace["tags"] if t.startswith("mcp:")], [])

    def test_mcp_attribution_unit_edge_cases(self):
        self.assertEqual(hook.mcp_attribution("mcp__github__get_pull_request"), ("github", "get_pull_request"))
        # Tool name containing "__" is preserved.
        self.assertEqual(hook.mcp_attribution("mcp__srv__a__b"), ("srv", "a__b"))
        self.assertIsNone(hook.mcp_attribution("mcp__github"))
        self.assertIsNone(hook.mcp_attribution("mcp____x"))
        self.assertIsNone(hook.mcp_attribution("Bash"))
        self.assertIsNone(hook.mcp_attribution(None))


if __name__ == "__main__":
    unittest.main(verbosity=2)
