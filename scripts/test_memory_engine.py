#!/usr/bin/env python3
"""Tests for memory-engine.py."""

import os
import sys
import tempfile
import shutil
from datetime import date, timedelta
from pathlib import Path

# Import from same directory
sys.path.insert(0, os.path.dirname(__file__))
import importlib
me = importlib.import_module("memory-engine")


# ─── parse_frontmatter / build_frontmatter roundtrip ─────────

def test_parse_simple():
    content = "---\ntype: note\nrelevance: 0.85\ntier: active\n---\n# Hello\nBody text"
    fields, body, had = me.parse_frontmatter(content)
    assert had is True
    assert fields["type"] == "note"
    assert fields["tier"] == "active"
    assert "# Hello" in body

def test_parse_no_yaml():
    content = "# Just a heading\nNo frontmatter here."
    fields, body, had = me.parse_frontmatter(content)
    assert had is False
    assert fields == {}
    assert body == content

def test_parse_multiline_description():
    """PyYAML handles multiline; fallback parser won't, but shouldn't crash."""
    content = '---\ntype: crm\ndescription: "Client: Acme Corp, deal $50K"\ntier: warm\n---\nBody'
    fields, body, had = me.parse_frontmatter(content)
    assert had is True
    assert "Acme" in fields.get("description", "")

def test_parse_tags_list():
    content = "---\ntype: note\ntags: [ai, ml, python]\ntier: active\n---\nBody"
    fields, body, had = me.parse_frontmatter(content)
    assert had is True
    # With PyYAML: list. Without: string "[ai, ml, python]"
    tags = fields.get("tags", "")
    if isinstance(tags, list):
        assert "ai" in tags
    else:
        assert "ai" in tags

def test_build_frontmatter_basic():
    fields = {"type": "note", "relevance": "0.5", "tier": "warm"}
    result = me.build_frontmatter(fields)
    assert result.startswith("---\n")
    assert result.endswith("---\n")
    assert "type: note" in result
    assert "tier: warm" in result

def test_build_frontmatter_special_chars():
    fields = {"type": "note", "title": "Note: important #1"}
    result = me.build_frontmatter(fields)
    assert "---" in result
    # Should be quoted due to special chars
    assert "Note" in result

def test_roundtrip():
    original = {"type": "crm", "relevance": "0.85", "tier": "active"}
    built = me.build_frontmatter(original) + "\n# Test Card\nBody."
    fields, body, had = me.parse_frontmatter(built)
    assert had is True
    assert fields["type"] == "crm"
    assert "0.85" in str(fields["relevance"])


# ─── calc_relevance / calc_tier ───────────────────────────────

def test_relevance_day_zero():
    assert me.calc_relevance(0, 0.015, 0.1) == 1.0

def test_relevance_day_7():
    r = me.calc_relevance(7, 0.015, 0.1)
    assert 0.88 <= r <= 0.92  # ~0.895

def test_relevance_floor():
    r = me.calc_relevance(100, 0.015, 0.1)
    assert r == 0.1

def test_relevance_negative_days():
    # Negative days shouldn't happen in practice (get_best_date uses max(0, ...))
    # but verify no crash. Result may exceed 1.0 since there's no upper clamp.
    r = me.calc_relevance(-5, 0.015, 0.1)
    assert r >= 1.0  # max(floor, 1.0 - (-5)*0.015) = 1.075

def test_tier_active():
    tiers = {"active": 7, "warm": 21, "cold": 60}
    assert me.calc_tier(3, tiers) == "active"

def test_tier_warm():
    tiers = {"active": 7, "warm": 21, "cold": 60}
    assert me.calc_tier(14, tiers) == "warm"

def test_tier_cold():
    tiers = {"active": 7, "warm": 21, "cold": 60}
    assert me.calc_tier(40, tiers) == "cold"

def test_tier_archive():
    tiers = {"active": 7, "warm": 21, "cold": 60}
    assert me.calc_tier(100, tiers) == "archive"

def test_tier_core_preserved():
    tiers = {"active": 7, "warm": 21, "cold": 60}
    assert me.calc_tier(100, tiers, "core") == "core"


# ─── infer_type / infer_title ─────────────────────────────────

def test_infer_type_match():
    p = Path("/vault/crm/clients/acme.md")
    assert me.infer_type(p, {"crm/": "crm"}) == "crm"

def test_infer_type_default():
    p = Path("/vault/random/file.md")
    assert me.infer_type(p, {"crm/": "crm"}) == "note"

def test_infer_title():
    assert me.infer_title("# My Title\nBody") == "My Title"
    assert me.infer_title("No heading here") == ""


# ─── cmd_init / cmd_decay with --dry-run ──────────────────────

def test_cmd_init_dry_run():
    tmpdir = Path(tempfile.mkdtemp())
    try:
        # Create a file without YAML
        (tmpdir / "test.md").write_text("# Test\nSome content", encoding="utf-8")
        config = me.DEFAULT_CONFIG.copy()
        config["use_git_dates"] = False  # no git in temp dir

        me.cmd_init(tmpdir, config, dry_run=True)

        # File should NOT be modified (dry run)
        content = (tmpdir / "test.md").read_text(encoding="utf-8")
        assert not content.startswith("---")
    finally:
        shutil.rmtree(tmpdir)

def test_cmd_init_writes():
    tmpdir = Path(tempfile.mkdtemp())
    try:
        (tmpdir / "test.md").write_text("# Test\nContent", encoding="utf-8")
        config = me.DEFAULT_CONFIG.copy()
        config["use_git_dates"] = False

        me.cmd_init(tmpdir, config, dry_run=False)

        content = (tmpdir / "test.md").read_text(encoding="utf-8")
        assert content.startswith("---")
        fields, body, had = me.parse_frontmatter(content)
        assert had is True
        assert "relevance" in fields
        assert "tier" in fields
    finally:
        shutil.rmtree(tmpdir)

def test_cmd_decay_updates():
    tmpdir = Path(tempfile.mkdtemp())
    try:
        old_date = (date.today() - timedelta(days=30)).isoformat()
        content = f"---\ntype: note\nlast_accessed: {old_date}\nrelevance: 1.0\ntier: active\n---\n# Old Card"
        (tmpdir / "old.md").write_text(content, encoding="utf-8")
        config = me.DEFAULT_CONFIG.copy()
        config["use_git_dates"] = False

        me.cmd_decay(tmpdir, config, dry_run=False)

        updated = (tmpdir / "old.md").read_text(encoding="utf-8")
        fields, _, _ = me.parse_frontmatter(updated)
        # After 30 days with rate 0.015: relevance should be ~0.55
        rel = float(fields["relevance"])
        assert rel < 0.7
        assert fields["tier"] in ("cold", "warm")
    finally:
        shutil.rmtree(tmpdir)


# ─── cmd_search ───────────────────────────────────────────────

def test_cmd_search():
    tmpdir = Path(tempfile.mkdtemp())
    try:
        c1 = "---\ntype: crm\ndescription: Acme Corporation client\ntier: active\nrelevance: 0.9\n---\n# Acme"
        c2 = "---\ntype: note\ndescription: Random note\ntier: warm\nrelevance: 0.5\n---\n# Other"
        (tmpdir / "acme.md").write_text(c1, encoding="utf-8")
        (tmpdir / "other.md").write_text(c2, encoding="utf-8")
        config = me.DEFAULT_CONFIG.copy()

        # Should find acme in normal mode
        me.cmd_search("Acme", tmpdir, config, "normal")
        # No assertion needed — just verify no crash. Output goes to stdout.

        # Heartbeat should not find warm-tier cards
        me.cmd_search("Random", tmpdir, config, "heartbeat")
    finally:
        shutil.rmtree(tmpdir)


# ─── cmd_promote ──────────────────────────────────────────────

def test_cmd_promote():
    tmpdir = Path(tempfile.mkdtemp())
    try:
        content = "---\ntype: note\ntier: warm\nrelevance: 0.6\n---\n# Test"
        filepath = tmpdir / "test.md"
        filepath.write_text(content, encoding="utf-8")

        me.cmd_promote(str(filepath), "core")

        updated = filepath.read_text(encoding="utf-8")
        fields, _, _ = me.parse_frontmatter(updated)
        assert fields["tier"] == "core"
        assert fields["relevance"] == "1.0"
    finally:
        shutil.rmtree(tmpdir)


# ─── estimate_tokens / extract_l1 / extract_level ────────────

def test_estimate_tokens():
    assert me.estimate_tokens("hello world") >= 2
    assert me.estimate_tokens("") == 0
    # ~4 bytes per token for ASCII
    assert me.estimate_tokens("a" * 400) == 100

def test_extract_l1_with_summary():
    body = "Some intro\n## Summary\nThis is the summary.\nMore summary.\n## Details\nFull details here."
    result = me.extract_l1(body)
    assert "This is the summary" in result
    assert "Full details" not in result

def test_extract_l1_fallback():
    body = " ".join(["word"] * 600)
    result = me.extract_l1(body)
    assert len(result.split()) == 500

def test_extract_level_l0():
    fields = {"description": "Test description"}
    assert me.extract_level(fields, "body text", 0) == "Test description"

def test_extract_level_l1_cached():
    fields = {"l1_summary": "Cached summary"}
    assert me.extract_level(fields, "body text", 1) == "Cached summary"

def test_extract_level_l2():
    fields = {}
    assert me.extract_level(fields, "full body", 2) == "full body"


# ─── compression helpers ─────────────────────────────────────

def test_extract_entities():
    text = "Met with @john about $50K deal. After lunch, John Smith and Alice Brown discussed terms."
    entities = me.extract_entities_heuristic(text)
    assert "@john" in entities
    assert "$50K" in entities

def test_extract_decisions():
    text = "We decided to go with option A.\nRandom line.\nThey agreed on the timeline.\nNo keywords here."
    decisions = me.extract_decisions(text)
    assert len(decisions) == 2
    assert "decided" in decisions[0].lower()

def test_extract_action_items():
    text = "- [ ] Call John\n- [x] Send proposal\nSome text\nTODO: review contract"
    items = me.extract_action_items(text)
    assert len(items) == 3

def test_compress_body():
    body = "First sentence. Second sentence. Third sentence. Fourth sentence.\nWe decided to proceed.\n- [ ] Follow up\n@alice mentioned $100K budget."
    result = me.compress_body(body)
    assert "## Summary" in result
    assert "Key Decisions" in result
    assert "Action Items" in result


# ─── cmd_compress / cmd_generate_l1 ──────────────────────────

def test_cmd_compress_skips_active():
    tmpdir = Path(tempfile.mkdtemp())
    try:
        content = "---\ntype: daily\ntier: active\nrelevance: 0.9\n---\n" + "x " * 5000
        (tmpdir / "2026-01-01.md").write_text(content, encoding="utf-8")
        config = me.DEFAULT_CONFIG.copy()
        me.cmd_compress(tmpdir, config, dry_run=True)
        # Should not compress active tier
        updated = (tmpdir / "2026-01-01.md").read_text(encoding="utf-8")
        assert "Key Decisions" not in updated
    finally:
        shutil.rmtree(tmpdir)

def test_cmd_compress_cold_daily():
    tmpdir = Path(tempfile.mkdtemp())
    try:
        body = "We decided to launch the product. " * 200 + "\n- [ ] Send report\n@bob mentioned $200K."
        content = f"---\ntype: daily\ntier: cold\nrelevance: 0.3\n---\n{body}"
        (tmpdir / "2025-12-01.md").write_text(content, encoding="utf-8")
        config = me.DEFAULT_CONFIG.copy()
        me.cmd_compress(tmpdir, config, dry_run=False)
        updated = (tmpdir / "2025-12-01.md").read_text(encoding="utf-8")
        assert "## Summary" in updated
        assert "Key Decisions" in updated
    finally:
        shutil.rmtree(tmpdir)

def test_cmd_generate_l1():
    tmpdir = Path(tempfile.mkdtemp())
    try:
        body = "## Summary\nThis is the L1 summary for the card.\n## Details\nLots of details here."
        content = f"---\ntype: note\ntier: active\nrelevance: 0.9\n---\n{body}"
        (tmpdir / "test.md").write_text(content, encoding="utf-8")
        config = me.DEFAULT_CONFIG.copy()
        me.cmd_generate_l1(tmpdir, config, dry_run=False)
        updated = (tmpdir / "test.md").read_text(encoding="utf-8")
        fields, _, _ = me.parse_frontmatter(updated)
        assert "l1_summary" in fields
        assert "L1 summary" in fields["l1_summary"]
        assert "content_tokens" in fields
    finally:
        shutil.rmtree(tmpdir)

def test_search_level_1():
    tmpdir = Path(tempfile.mkdtemp())
    try:
        content = "---\ntype: crm\ndescription: Acme Corp\nl1_summary: FMCG client with $66K deal\ntier: active\nrelevance: 0.9\n---\n# Acme"
        (tmpdir / "acme.md").write_text(content, encoding="utf-8")
        config = me.DEFAULT_CONFIG.copy()
        # Should not crash with level=1
        me.cmd_search("Acme", tmpdir, config, "normal", level=1)
    finally:
        shutil.rmtree(tmpdir)

def test_backward_compat_no_new_fields():
    """Cards without l1_summary and content_tokens should work fine."""
    tmpdir = Path(tempfile.mkdtemp())
    try:
        content = "---\ntype: note\ntier: warm\nrelevance: 0.5\n---\n# Old Card\nNo new fields."
        (tmpdir / "old.md").write_text(content, encoding="utf-8")
        config = me.DEFAULT_CONFIG.copy()
        config["use_git_dates"] = False
        # All commands should work without crashing
        me.cmd_scan(tmpdir, config)
        me.cmd_search("Old", tmpdir, config, "normal", level=0)
        me.cmd_stats(tmpdir, config)
    finally:
        shutil.rmtree(tmpdir)


# ─── run all tests ────────────────────────────────────────────

def run_tests():
    tests = [v for k, v in globals().items() if k.startswith("test_") and callable(v)]
    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
            print(f"  PASS  {test.__name__}")
        except Exception as e:
            failed += 1
            print(f"  FAIL  {test.__name__}: {e}")

    print(f"\n  results: {passed} passed, {failed} failed, {passed + failed} total")
    return failed == 0


if __name__ == "__main__":
    success = run_tests()
    sys.exit(0 if success else 1)
