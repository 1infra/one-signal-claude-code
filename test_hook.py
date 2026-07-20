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

    def test_anthropic_sk_ant_tagged_not_openai(self):
        # betterleaks anthropic-api-key: sk-ant-api03-{93}AA
        tok = "sk-ant-api03-" + ("a" * 93) + "AA"
        out = hook.redact_text(f"ANTHROPIC_API_KEY={tok}")
        self.assertIn("<REDACTED:anthropic>", out)
        self.assertNotIn("<REDACTED:openai>", out)
        self.assertNotIn(tok, out)

    def test_openrouter_sk_or_tagged_not_openai(self):
        # betterleaks openrouter-api-key: sk-or-v1-{64 hex}
        tok = "sk-or-v1-" + ("0" * 64)
        out = hook.redact_text(f"OPENROUTER_API_KEY={tok}")
        self.assertIn("<REDACTED:openrouter>", out)
        self.assertNotIn("<REDACTED:openai>", out)
        self.assertNotIn(tok, out)

    def test_figma_token(self):
        tok = "figd_" + ("A" * 40)
        out = hook.redact_text(f"FIGMA_TOKEN={tok}")
        self.assertIn("<REDACTED:figma>", out)
        self.assertNotIn(tok, out)

    def test_npm_token(self):
        tok = "npm_" + ("b" * 36)
        out = hook.redact_text(f"NPM_TOKEN={tok}")
        self.assertIn("<REDACTED:npm>", out)
        self.assertNotIn(tok, out)

    def test_gitlab_pat(self):
        tok = "glpat-" + ("c" * 20)
        out = hook.redact_text(f"GITLAB_TOKEN={tok}")
        self.assertIn("<REDACTED:gitlab>", out)
        self.assertNotIn(tok, out)

    def test_huggingface_token(self):
        tok = "hf_" + ("d" * 34)
        out = hook.redact_text(f"HF_TOKEN={tok}")
        self.assertIn("<REDACTED:huggingface>", out)
        self.assertNotIn(tok, out)

    def test_supabase_tokens(self):
        sbp = "sbp_" + ("e" * 40)
        secret = "sb_secret_" + ("f" * 31)
        out = hook.redact_text(f"{sbp} {secret}")
        self.assertEqual(out.count("<REDACTED:supabase>"), 2)
        self.assertNotIn(sbp, out)
        self.assertNotIn(secret, out)

    def test_shopify_tokens(self):
        toks = [
            "shpat_" + ("1" * 32),
            "shpca_" + ("2" * 32),
            "shppa_" + ("3" * 32),
            "shpss_" + ("4" * 32),
        ]
        out = hook.redact_text(" ".join(toks))
        self.assertEqual(out.count("<REDACTED:shopify>"), 4)
        for t in toks:
            self.assertNotIn(t, out)

    def test_digitalocean_tokens(self):
        toks = [
            "dop_v1_" + ("a" * 64),
            "doo_v1_" + ("b" * 64),
            "dor_v1_" + ("c" * 64),
        ]
        out = hook.redact_text(" ".join(toks))
        self.assertEqual(out.count("<REDACTED:digitalocean>"), 3)
        for t in toks:
            self.assertNotIn(t, out)

    def test_databricks_token(self):
        tok = "dapi" + ("a" * 32)
        out = hook.redact_text(f"DATABRICKS_TOKEN={tok}")
        self.assertIn("<REDACTED:databricks>", out)
        self.assertNotIn(tok, out)

    def test_sendgrid_token(self):
        tok = "SG." + ("A" * 66)
        out = hook.redact_text(f"SENDGRID_API_KEY={tok}")
        self.assertIn("<REDACTED:sendgrid>", out)
        self.assertNotIn(tok, out)

    def test_telegram_bot_token(self):
        # telegram-bot-api-token shape: {5,16 digits}:A{34 body}
        tok = "123456789:A" + ("B" * 34)
        out = hook.redact_text(f"BOT_TOKEN={tok}")
        self.assertIn("<REDACTED:telegram>", out)
        self.assertNotIn(tok, out)

    def test_airtable_pat(self):
        tok = "pat" + ("A" * 14) + "." + ("a" * 64)
        out = hook.redact_text(f"AIRTABLE_PAT={tok}")
        self.assertIn("<REDACTED:airtable>", out)
        self.assertNotIn(tok, out)

    def test_grafana_tokens(self):
        glc = "glc_" + ("A" * 40) + "=="
        glsa = "glsa_" + ("B" * 32) + "_" + ("c" * 8)
        out = hook.redact_text(f"{glc} {glsa}")
        self.assertEqual(out.count("<REDACTED:grafana>"), 2)
        self.assertNotIn(glc, out)
        self.assertNotIn(glsa, out)

    def test_sentry_tokens(self):
        # sntrys_eyJpYXQiO… distinctive prefix from sentry-org-token
        sntrys = "sntrys_eyJpYXQiO" + ("A" * 40) + "_" + ("B" * 43)
        sntryu = "sntryu_" + ("a" * 64)
        out = hook.redact_text(f"{sntrys} {sntryu}")
        self.assertEqual(out.count("<REDACTED:sentry>"), 2)
        self.assertNotIn(sntrys, out)
        self.assertNotIn(sntryu, out)

    def test_fly_fo1_token(self):
        tok = "fo1_" + ("x" * 43)
        out = hook.redact_text(f"FLY_API_TOKEN={tok}")
        self.assertIn("<REDACTED:fly>", out)
        self.assertNotIn(tok, out)

    def test_groq_token(self):
        tok = "gsk_" + ("A" * 52)
        out = hook.redact_text(f"GROQ_API_KEY={tok}")
        self.assertIn("<REDACTED:groq>", out)
        self.assertNotIn(tok, out)

    def test_xai_token(self):
        tok = "xai-" + ("A" * 80)
        out = hook.redact_text(f"XAI_API_KEY={tok}")
        self.assertIn("<REDACTED:xai>", out)
        self.assertNotIn(tok, out)

    def test_perplexity_token(self):
        tok = "pplx-" + ("A" * 48)
        out = hook.redact_text(f"PPLX_API_KEY={tok}")
        self.assertIn("<REDACTED:perplexity>", out)
        self.assertNotIn(tok, out)

    def test_replicate_token(self):
        tok = "r8_" + ("A" * 37)
        out = hook.redact_text(f"REPLICATE_API_TOKEN={tok}")
        self.assertIn("<REDACTED:replicate>", out)
        self.assertNotIn(tok, out)

    def test_doppler_token(self):
        tok = "dp.pt." + ("a" * 43)
        out = hook.redact_text(f"DOPPLER_TOKEN={tok}")
        self.assertIn("<REDACTED:doppler>", out)
        self.assertNotIn(tok, out)

    def test_linear_token(self):
        tok = "lin_api_" + ("a" * 40)
        out = hook.redact_text(f"LINEAR_API_KEY={tok}")
        self.assertIn("<REDACTED:linear>", out)
        self.assertNotIn(tok, out)

    def test_notion_token(self):
        # ntn_ + 11 digits + 35 alnum (betterleaks notion-api-token)
        tok = "ntn_" + ("1" * 11) + ("A" * 35)
        out = hook.redact_text(f"NOTION_TOKEN={tok}")
        self.assertIn("<REDACTED:notion>", out)
        self.assertNotIn(tok, out)

    def test_postman_token(self):
        tok = "PMAK-" + ("a" * 24) + "-" + ("b" * 34)
        out = hook.redact_text(f"POSTMAN_API_KEY={tok}")
        self.assertIn("<REDACTED:postman>", out)
        self.assertNotIn(tok, out)

    def test_1password_service_account_token(self):
        # ops_eyJ + ≥250 base64 body (1password-service-account-token)
        tok = "ops_eyJ" + ("A" * 250) + "=="
        out = hook.redact_text(f"OP_SERVICE_ACCOUNT_TOKEN={tok}")
        self.assertIn("<REDACTED:1password>", out)
        self.assertNotIn(tok, out)

    def test_vercel_tokens(self):
        toks = [
            "vcp_" + ("A" * 56),
            "vck_" + ("B" * 56),
            "vci_" + ("C" * 56),
        ]
        out = hook.redact_text(" ".join(toks))
        self.assertEqual(out.count("<REDACTED:vercel>"), 3)
        for t in toks:
            self.assertNotIn(t, out)

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

    def test_sk_ant_lookalike_too_short_untouched(self):
        # Too short for anthropic shape AND for generic sk- {20,} body.
        lookalike = "sk-ant-short"
        self.assertEqual(hook.redact_text(lookalike), lookalike)

    def test_sk_or_lookalike_too_short_untouched(self):
        lookalike = "sk-or-v1-deadbeef"
        self.assertEqual(hook.redact_text(lookalike), lookalike)

    def test_figma_lookalike_too_short_untouched(self):
        lookalike = "figd_short"
        self.assertEqual(hook.redact_text(lookalike), lookalike)

    def test_npm_lookalike_too_short_untouched(self):
        lookalike = "npm_notalongenoughtoken"
        self.assertEqual(hook.redact_text(lookalike), lookalike)

    def test_gitlab_lookalike_too_short_untouched(self):
        lookalike = "glpat-short"
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
