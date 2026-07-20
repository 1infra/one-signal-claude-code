#!/usr/bin/env python3
"""Unit tests for one_signal_hook.py's skill/MCP attribution.

No network calls. Run with:

    uv run python plugins/one-signal/test_hook.py

From the repo root, `pnpm test:plugins` runs this suite and the
one-signal-codex one together.
"""
import importlib.util
import hashlib
import json
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

    def test_uploads_only_claude_instructions_and_preserves_symlink_identity(self):
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
            (project / "CLAUDE.md").symlink_to("AGENTS.md")
            (nested / "CLAUDE.md").write_text("nested claude", encoding="utf-8")
            turn = make_turn("done")
            turn.user_msg["cwd"] = str(nested)

            with mock.patch.object(hook.Path, "home", return_value=home):
                trace = self._trace(turn)

            documents = trace["metadata"]["instruction_documents"]
            self.assertEqual([document["path"] for document in documents], [
                "~/.claude/CLAUDE.md",
                "CLAUDE.md",
                "packages/app/CLAUDE.md",
            ])
            self.assertEqual(documents[1], {
                "agent": "claude-code",
                "path": "CLAUDE.md",
                "scope": "project",
                "directory_scope": ".",
                "content": "project agents",
                "content_hash": hashlib.sha256(b"project agents").hexdigest(),
            })

    def test_later_turn_uploads_only_new_nested_instruction_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            project = home / "project"
            nested = project / "packages" / "app"
            (project / ".git").mkdir(parents=True)
            nested.mkdir(parents=True)
            (home / ".claude").mkdir()
            (project / "CLAUDE.md").write_text("root rules", encoding="utf-8")
            (nested / "CLAUDE.md").write_text("nested rules", encoding="utf-8")
            first = make_turn("done")
            first.user_msg["cwd"] = str(project)
            later = make_turn([{
                "type": "tool_use",
                "id": "touch-nested",
                "name": "Read",
                "input": {"file_path": str(nested / "src.ts")},
            }])
            later.user_msg["cwd"] = str(project)
            known: set[str] = set()

            with mock.patch.object(hook.Path, "home", return_value=home):
                first_events = hook.build_turn_events("session-1", 1, first, Path("transcript.jsonl"), known_instruction_documents=known)
                later_events = hook.build_turn_events("session-1", 2, later, Path("transcript.jsonl"), known_instruction_documents=known)

            first_documents = next(event for event in first_events if event["type"] == "trace-create")["body"]["metadata"]["instruction_documents"]
            later_documents = next(event for event in later_events if event["type"] == "trace-create")["body"]["metadata"]["instruction_documents"]
            self.assertEqual([document["path"] for document in first_documents], ["CLAUDE.md"])
            self.assertEqual([document["path"] for document in later_documents], ["packages/app/CLAUDE.md"])

    def test_records_images_omitted_from_session_text(self):
        turn = make_turn("done")
        turn.user_msg["message"]["content"] = [
            {"type": "text", "text": "inspect this"},
            {"type": "image", "source": {"type": "base64", "data": "abc"}},
        ]

        trace = self._trace(turn)
        self.assertEqual(trace["metadata"]["omitted_image_count"], 1)

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
        # Failed tool SPANs must set observation-level level=ERROR so the
        # server's errorRate metric (share of level==ERROR) is non-zero.
        self.assertEqual(tool["level"], "ERROR")

    def test_successful_tool_span_omits_level(self):
        turn = make_turn([
            {"type": "tool_use", "id": "tool-1", "name": "Bash", "input": {"command": "ls"}},
        ])
        turn.tool_results_by_id = {
            "tool-1": {"content": "ok", "timestamp": "2026-07-12T00:00:02Z"},
        }
        events = hook.build_turn_events("session-1", 1, turn, Path("transcript.jsonl"))
        tool = next(
            event["body"] for event in events
            if (event["body"].get("metadata") or {}).get("tool_name") == "Bash"
        )
        self.assertEqual(tool["metadata"]["result_status"], "success")
        # Unset level is coerced to DEFAULT server-side; do not send DEFAULT
        # explicitly, and do not set level on successful tools.
        self.assertNotIn("level", tool)

    def test_mcp_tool_error_sets_observation_level_error(self):
        rows = [
            ({"type": "user", "timestamp": "2026-07-12T00:00:00Z", "message": {"content": "get pr"}}, 1),
            ({
                "type": "assistant",
                "timestamp": "2026-07-12T00:00:01Z",
                "message": {
                    "id": "msg-1",
                    "content": [{
                        "type": "tool_use",
                        "id": "mcp-1",
                        "name": "mcp__github__get_pull_request",
                        "input": {"number": 42},
                    }],
                },
            }, 2),
            ({
                "type": "user",
                "timestamp": "2026-07-12T00:00:02Z",
                "message": {"content": [{
                    "type": "tool_result",
                    "tool_use_id": "mcp-1",
                    "content": "not found",
                    "is_error": True,
                }]},
            }, 3),
        ]
        turn = hook.build_turns(rows)[0]
        events = hook.build_turn_events("session-1", 1, turn, Path("transcript.jsonl"))
        mcp = next(
            event["body"] for event in events
            if (event["body"].get("metadata") or {}).get("mcp_server") == "github"
        )
        self.assertEqual(mcp["metadata"]["result_status"], "error")
        self.assertEqual(mcp["level"], "ERROR")

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


class TestRedactText(unittest.TestCase):
    """Pre-upload secret redaction: known-format tokens, URI passwords, idempotency."""

    # --- Positive: each token class → correct class tag ---

    def test_aws_access_key_id(self):
        raw = "export AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE"
        out = hook.redact_text(raw)
        self.assertIn("<REDACTED:aws>", out)
        self.assertNotIn("AKIAIOSFODNN7EXAMPLE", out)

    def test_github_pat_and_token_prefix(self):
        ghp = "ghp_" + ("a" * 36)
        fine = "github_pat_" + ("b" * 22)
        out = hook.redact_text(f"token={ghp} fine={fine}")
        self.assertEqual(out.count("<REDACTED:github>"), 2)
        self.assertNotIn(ghp, out)
        self.assertNotIn(fine, out)

    def test_openai_anthropic_sk_style(self):
        sk = "sk-" + ("x" * 20)
        out = hook.redact_text(f"OPENAI_API_KEY={sk}")
        self.assertIn("<REDACTED:openai>", out)
        self.assertNotIn(sk, out)

    def test_slack_token(self):
        tok = "xoxb-" + ("1" * 12)
        out = hook.redact_text(f"SLACK_BOT_TOKEN={tok}")
        self.assertIn("<REDACTED:slack>", out)
        self.assertNotIn(tok, out)

    def test_google_api_key(self):
        key = "AIza" + ("C" * 35)
        out = hook.redact_text(f"key={key}")
        self.assertIn("<REDACTED:google>", out)
        self.assertNotIn(key, out)

    def test_stripe_key(self):
        live = "sk_live_" + ("d" * 16)
        test = "rk_test_" + ("e" * 16)
        out = hook.redact_text(f"{live} {test}")
        self.assertEqual(out.count("<REDACTED:stripe>"), 2)
        self.assertNotIn(live, out)
        self.assertNotIn(test, out)

    def test_jwt(self):
        jwt = "eyJhbGciOiJIUzI1NiJ9." + ("a" * 12) + "." + ("b" * 8)
        out = hook.redact_text(f"Bearer {jwt}")
        self.assertIn("<REDACTED:jwt>", out)
        self.assertNotIn(jwt, out)

    def test_pem_private_key_block(self):
        pem = (
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEowIBAAKCAQEA0Z3VS5JJcds3xfn/ygWyF6PZGBw=\n"
            "-----END RSA PRIVATE KEY-----"
        )
        out = hook.redact_text(f"key material:\n{pem}\ndone")
        self.assertIn("<REDACTED:pem>", out)
        self.assertNotIn("BEGIN RSA PRIVATE KEY", out)
        self.assertNotIn("MIIEowIBAAKCAQEA", out)
        self.assertIn("key material:", out)
        self.assertIn("done", out)

    def test_db_connection_string_masks_only_password(self):
        raw = "postgres://alice:s3cret-pass@db.example.com:5432/app"
        out = hook.redact_text(raw)
        self.assertEqual(out, "postgres://alice:<REDACTED>@db.example.com:5432/app")
        self.assertNotIn("s3cret-pass", out)

    def test_mongodb_srv_and_redis_connection_strings(self):
        mongo = "mongodb+srv://u:p@cluster0.example.net/db"
        redis = "rediss://cache:hunter2@redis.internal:6380/0"
        self.assertEqual(
            hook.redact_text(mongo),
            "mongodb+srv://u:<REDACTED>@cluster0.example.net/db",
        )
        self.assertEqual(
            hook.redact_text(redis),
            "rediss://cache:<REDACTED>@redis.internal:6380/0",
        )

    def test_generic_uri_with_embedded_password(self):
        raw = "https://deploy:topsecret@ci.example.com/hooks"
        out = hook.redact_text(raw)
        self.assertEqual(out, "https://deploy:<REDACTED>@ci.example.com/hooks")
        self.assertNotIn("topsecret", out)

    # --- Negative: lookalikes / plain URLs stay intact ---

    def test_sk_lookalike_without_word_boundary_length_is_untouched(self):
        # Word-boundary + length bound: short "sk-" fragments in skill names
        # must not fire (no entropy scan, known-format only).
        lookalike = "skill-name-with-sk-prefix"
        self.assertEqual(hook.redact_text(lookalike), lookalike)

    def test_plain_url_without_password_unchanged(self):
        url = "https://example.com/path?q=1"
        self.assertEqual(hook.redact_text(url), url)

    def test_non_secret_text_unchanged(self):
        plain = "please run the tests and open a PR"
        self.assertEqual(hook.redact_text(plain), plain)

    # --- Idempotency ---

    def test_idempotent_double_pass(self):
        raw = (
            "aws=AKIAIOSFODNN7EXAMPLE "
            "sk=" + ("sk-" + "z" * 24) + " "
            "db=postgres://u:pass@host/db "
            "url=https://user:pw@api.example.com/v1"
        )
        once = hook.redact_text(raw)
        twice = hook.redact_text(once)
        self.assertEqual(once, twice)
        # Placeholders themselves must not be re-eaten / nested.
        self.assertNotIn("<REDACTED:<REDACTED", twice)
        self.assertIn("<REDACTED:aws>", twice)
        self.assertIn("<REDACTED:openai>", twice)
        self.assertIn("postgres://u:<REDACTED>@host/db", twice)
        self.assertIn("https://user:<REDACTED>@api.example.com/v1", twice)

    def test_already_redacted_placeholder_preserved(self):
        raw = "token=<REDACTED:aws> still ok"
        self.assertEqual(hook.redact_text(raw), raw)

    # --- End-to-end via real build_turn_events pipeline ---

    def test_tool_span_body_masks_secret_via_pipeline(self):
        secret = "sk-" + ("pipeline" + "0" * 20)  # length-bounded sk- token
        turn = make_turn([
            {
                "type": "tool_use",
                "id": "tool-1",
                "name": "Bash",
                "input": {"command": f"echo {secret}"},
            },
        ])
        turn.user_msg["message"]["content"] = f"run with key {secret}"
        turn.tool_results_by_id = {
            "tool-1": {
                "content": f"stdout: using {secret}",
                "timestamp": "2026-07-12T00:00:02Z",
            },
        }
        events = hook.build_turn_events("session-1", 1, turn, Path("transcript.jsonl"))
        tool = next(
            event["body"] for event in events
            if (event["body"].get("metadata") or {}).get("tool_name") == "Bash"
        )
        # Free-text fields on the tool span (input + output) must be masked.
        self.assertNotIn(secret, json.dumps(tool["input"]))
        self.assertNotIn(secret, json.dumps(tool["output"]))
        self.assertIn("<REDACTED:openai>", json.dumps(tool["input"]))
        self.assertIn("<REDACTED:openai>", json.dumps(tool["output"]))
        # Trace user/assistant free text also redacted; ids/metadata keys untouched.
        trace = next(e["body"] for e in events if e["type"] == "trace-create")
        self.assertNotIn(secret, json.dumps(trace["input"]))
        self.assertIn("<REDACTED:openai>", json.dumps(trace["input"]))
        self.assertEqual(tool["metadata"]["tool_name"], "Bash")
        self.assertEqual(tool["metadata"]["tool_id"], "tool-1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
