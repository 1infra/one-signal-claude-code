#!/usr/bin/env python3
import importlib.util
import unittest
from pathlib import Path


HOOK_PATH = Path(__file__).resolve().parent / "hooks" / "one_signal_hook.py"
SPEC = importlib.util.spec_from_file_location("one_signal_hook", HOOK_PATH)
assert SPEC and SPEC.loader
hook = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hook)


class TestAttribution(unittest.TestCase):
    def test_build_turn_events_attributes_skills_and_mcp_tools(self):
        turn = hook.Turn(
            user_msg={"timestamp": "2026-07-12T00:00:00Z", "message": {"content": "review this"}},
            assistant_msgs=[{
                "timestamp": "2026-07-12T00:00:01Z",
                "message": {
                    "id": "msg-1",
                    "content": [
                        {"type": "tool_use", "id": "skill-1", "name": "Skill", "input": {"name": "code-review"}},
                        {"type": "tool_use", "id": "mcp-1", "name": "mcp__github__get_pull_request", "input": {"number": 42}},
                    ],
                },
            }],
            tool_results_by_id={"mcp-1": {"content": "pull request 42", "timestamp": "2026-07-12T00:00:02Z"}},
            injected_by_tool_id={},
            end_offset=1,
        )

        events = hook.build_turn_events("session-1", 1, turn, Path("transcript.jsonl"))
        trace = next(event for event in events if event["type"] == "trace-create")["body"]
        mcp = next(
            event["body"] for event in events
            if (event["body"].get("metadata") or {}).get("mcp_server") == "github"
        )

        self.assertEqual(trace["metadata"]["skill_names"], ["code-review"])
        self.assertIn("skill:code-review", trace["tags"])
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
