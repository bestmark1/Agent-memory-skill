#!/usr/bin/env python3
"""
memory-engine.py — Universal memory management for AI agents.

Implements Ebbinghaus-inspired forgetting curve with tiered recall.
Works with any directory of markdown files.

Commands:
  scan        [dir]           Analyze files, report stats (no changes)
  init        [dir]           Add YAML frontmatter to files missing it
  decay       [dir]           Update relevance scores and tiers
  touch       <file>          Reset file to active (on read/use)
  search      <query> [dir]   Search cards by text, filtered by tier
  promote     <file> <tier>   Set card tier (e.g., promote to core)
  demote      <file> <tier>   Set card tier (e.g., demote to archive)
  creative    <N> [dir]       Random N cards from cold/archive tiers
  stats       [dir]           Show tier distribution and health metrics
  compress    [dir]           Compress old daily files (cold/archive tiers)
  generate-l1 [dir]           Generate L1 summaries from card bodies

Options:
  --config <path>         Config JSON (default: .memory-config.json in target dir)
  --dry-run               Preview changes without writing
  --verbose               Show per-file details
  --level 0|1|2           Search detail level (0=description, 1=summary, 2=full)
  --all                   Compress non-daily cards too (with compress)

Config (.memory-config.json):
{
  "tiers": {
    "active": 7,          // days threshold
    "warm": 21,
    "cold": 60
    // beyond cold = archive
  },
  "decay_rate": 0.015,    // relevance loss per day (linear)
  "relevance_floor": 0.1, // minimum relevance
  "skip_patterns": ["_index.md", "MOC-*"],
  "type_inference": {
    "crm/": "crm",
    "leads/": "lead",
    "personal/": "personal"
  },
  "use_git_dates": true,
  "context_budget": {
    "hot_limit_kb": 4,
    "active_limit_kb": 50,
    "total_warn_kb": 500
  }
}
"""

import os
import re
import sys
import json
import random
import subprocess
from datetime import datetime, date
from pathlib import Path
from typing import Optional

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False

# ─── defaults ───────────────────────────────────────────────────

DEFAULT_CONFIG = {
    "tiers": {"active": 7, "warm": 21, "cold": 60},
    "decay_rate": 0.015,
    "relevance_floor": 0.1,
    "skip_patterns": ["_index.md"],
    "type_inference": {},
    "use_git_dates": True,
    "context_budget": {
        "hot_limit_kb": 4,
        "active_limit_kb": 50,
        "total_warn_kb": 500,
    },
}

TODAY = date.today()

# ─── YAML frontmatter parsing ──────────────────────────────────

def _parse_frontmatter_simple(yaml_block: str) -> dict:
    """Fallback parser: line-by-line key: value (no PyYAML)."""
    fields = {}
    for line in yaml_block.split("\n"):
        if ":" in line:
            key, _, val = line.partition(":")
            fields[key.strip()] = val.strip()
    return fields


def parse_frontmatter(content: str) -> tuple[dict, str, bool]:
    """Parse YAML frontmatter. Returns (fields, body, had_yaml).
    Uses PyYAML when available for correct handling of multiline values,
    lists, and special characters. Falls back to simple parser otherwise.
    """
    if content.startswith("---\n"):
        end = content.find("\n---\n", 4)
        if end != -1:
            yaml_block = content[4:end]
            body = content[end + 5:]
            if HAS_YAML:
                try:
                    parsed = yaml.safe_load(yaml_block)
                    if isinstance(parsed, dict):
                        # Normalize all values to strings for consistency
                        fields = {}
                        for k, v in parsed.items():
                            if isinstance(v, list):
                                fields[k] = v  # preserve lists
                            elif v is None:
                                fields[k] = ""
                            else:
                                fields[k] = str(v) if not isinstance(v, str) else v
                        return fields, body, True
                except yaml.YAMLError:
                    pass  # fall through to simple parser
            fields = _parse_frontmatter_simple(yaml_block)
            return fields, body, True
    return {}, content, False


def _yaml_format_value(val) -> str:
    """Format a value for YAML output, quoting when necessary."""
    if isinstance(val, list):
        items = ", ".join(str(i) for i in val)
        return f"[{items}]"
    s = str(val)
    # Quote if value contains YAML-special characters
    if any(c in s for c in (":", "#", "'", '"', "{", "}", "[", "]", ">", "|", "\n")):
        escaped = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return s


def build_frontmatter(fields: dict, field_order: list[str] | None = None) -> str:
    """Build YAML frontmatter from dict, preserving field order.
    Properly quotes values containing special YAML characters.
    """
    if field_order is None:
        field_order = [
            "type", "title", "description", "l1_summary", "tags",
            "industry", "source", "priority", "status", "region",
            "owner", "responsible", "domain", "related", "client",
            "deal_status", "deal_deadline", "deadline",
            "created", "updated", "last_accessed",
            "content_tokens", "relevance", "tier",
        ]
    lines = []
    used = set()
    for key in field_order:
        if key in fields:
            lines.append(f"{key}: {_yaml_format_value(fields[key])}")
            used.add(key)
    for key, val in fields.items():
        if key not in used:
            lines.append(f"{key}: {_yaml_format_value(val)}")
    return "---\n" + "\n".join(lines) + "\n---\n"


# ─── date resolution ───────────────────────────────────────────

def get_git_date(filepath: Path) -> date | None:
    """Last git commit date for file."""
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%aI", "--", str(filepath)],
            capture_output=True, text=True,
            cwd=filepath.parent, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return date.fromisoformat(result.stdout.strip()[:10])
    except Exception:
        pass
    return None


def get_best_date(fields: dict, filepath: Path, use_git: bool = True) -> date:
    """Most recent date from YAML fields, git history, or file mtime."""
    candidates = []
    for field in ["last_accessed", "updated", "created"]:
        val = fields.get(field, "")
        try:
            candidates.append(date.fromisoformat(val[:10]))
        except (ValueError, IndexError):
            continue
    if use_git:
        git_date = get_git_date(filepath)
        if git_date:
            candidates.append(git_date)
    if not candidates:
        mtime = os.path.getmtime(filepath)
        candidates.append(date.fromtimestamp(mtime))
    return max(candidates)


# ─── core logic ─────────────────────────────────────────────────

def calc_relevance(days: int, rate: float, floor: float) -> float:
    """Linear decay with floor."""
    return round(max(floor, 1.0 - days * rate), 2)


def calc_tier(days: int, tiers: dict, current_tier: str = "") -> str:
    """Assign tier based on days since last access."""
    if current_tier == "core":
        return "core"  # never auto-demote core
    sorted_tiers = sorted(tiers.items(), key=lambda x: x[1])
    for tier_name, threshold in sorted_tiers:
        if days <= threshold:
            return tier_name
    return "archive"


def infer_type(filepath: Path, type_map: dict) -> str:
    """Infer card type from path using configurable mapping."""
    path_str = str(filepath)
    for pattern, card_type in type_map.items():
        if pattern in path_str:
            return card_type
    return "note"


def should_skip(filepath: Path, patterns: list[str]) -> bool:
    """Check if file matches skip patterns."""
    import fnmatch
    name = filepath.name
    for pattern in patterns:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


def infer_title(body: str) -> str:
    """Extract title from first H1 heading."""
    for line in body.split("\n")[:10]:
        if line.startswith("# "):
            return line[2:].strip()
    return ""


# ─── L0/L1/L2 hierarchical loading ──────────────────────────────

def estimate_tokens(text: str) -> int:
    """Heuristic token count: ~4 bytes per token."""
    return len(text.encode("utf-8")) // 4


def extract_l1(body: str) -> str:
    """Extract L1 summary from body.

    Looks for ## Summary section first. Falls back to first 500 words.
    """
    lines = body.split("\n")
    in_summary = False
    summary_lines = []
    for line in lines:
        if re.match(r"^##\s+[Ss]ummary", line):
            in_summary = True
            continue
        if in_summary:
            if line.startswith("## "):
                break
            summary_lines.append(line)
    if summary_lines:
        return "\n".join(summary_lines).strip()
    # Fallback: first 500 words
    words = body.split()
    if len(words) > 500:
        return " ".join(words[:500])
    return body.strip()


def extract_level(fields: dict, body: str, level: int) -> str:
    """Return content at the requested detail level.

    L0 = description (one line)
    L1 = l1_summary or extracted summary
    L2 = full body
    """
    if level == 0:
        return fields.get("description", "")
    if level == 1:
        cached = fields.get("l1_summary", "")
        if cached:
            return cached
        return extract_l1(body)
    return body


# ─── compression helpers ─────────────────────────────────────────

def extract_entities_heuristic(text: str) -> list[str]:
    """Extract entities from text using regex heuristics.

    Finds: capitalized multi-word names, @mentions, dollar amounts.
    """
    entities = set()
    # @mentions
    for m in re.finditer(r"@(\w+)", text):
        entities.add("@" + m.group(1))
    # Dollar amounts
    for m in re.finditer(r"\$[\d,.]+[KMBkmb]?", text):
        entities.add(m.group(0))
    # Capitalized phrases (2+ words, not at line start)
    for m in re.finditer(r"(?:^|[.!?,;]\s+)([A-ZА-ЯЁ][a-zа-яё]+(?:\s+[A-ZА-ЯЁ][a-zа-яё]+)+)", text, re.MULTILINE):
        entities.add(m.group(1))
    return sorted(entities)


def extract_decisions(text: str) -> list[str]:
    """Extract decision-like lines from text."""
    keywords = [
        "decided", "agreed", "will ", "must ", "chosen", "approved", "rejected",
        "решили", "согласовали", "надо ", "выбрали", "утвердили", "отклонили",
    ]
    decisions = []
    for line in text.split("\n"):
        stripped = line.strip()
        if not stripped or len(stripped) < 10:
            continue
        lower = stripped.lower()
        if any(kw in lower for kw in keywords):
            decisions.append(stripped)
    return decisions[:20]


def extract_action_items(text: str) -> list[str]:
    """Extract action items (checkboxes, TODOs) from text."""
    items = []
    for line in text.split("\n"):
        stripped = line.strip()
        if re.match(r"^- \[[ x]\]", stripped):
            items.append(stripped)
        elif stripped.upper().startswith("TODO") or stripped.upper().startswith("ACTION"):
            items.append(stripped)
    return items[:20]


def compress_body(body: str) -> str:
    """Compress a card body by extracting key information.

    Returns structured markdown with summary, decisions, entities, and actions.
    """
    # First 3 sentences as summary
    sentences = re.split(r"(?<=[.!?])\s+", body.strip())
    summary = " ".join(sentences[:3]) if sentences else ""

    decisions = extract_decisions(body)
    entities = extract_entities_heuristic(body)
    actions = extract_action_items(body)

    parts = []
    if summary:
        parts.append(f"## Summary\n{summary}")
    if decisions:
        parts.append("### Key Decisions\n" + "\n".join(f"- {d}" for d in decisions))
    if entities:
        parts.append("### Entities Mentioned\n- " + ", ".join(entities))
    if actions:
        parts.append("### Action Items\n" + "\n".join(actions))

    return "\n\n".join(parts) + "\n" if parts else body


# ─── config ─────────────────────────────────────────────────────

def load_config(target_dir: Path, config_path: str | None = None) -> dict:
    """Load config from file or return defaults."""
    if config_path:
        p = Path(config_path)
    else:
        p = target_dir / ".memory-config.json"
    if p.exists():
        with open(p) as f:
            user = json.load(f)
        # Merge with defaults
        config = {**DEFAULT_CONFIG, **user}
        config["tiers"] = {**DEFAULT_CONFIG["tiers"], **user.get("tiers", {})}
        return config
    return DEFAULT_CONFIG.copy()


def save_default_config(target_dir: Path):
    """Write default config file."""
    p = target_dir / ".memory-config.json"
    with open(p, "w") as f:
        json.dump(DEFAULT_CONFIG, f, indent=2)
    print(f"  wrote default config: {p}")


# ─── commands ───────────────────────────────────────────────────

def find_cards(target_dir: Path, config: dict) -> list[Path]:
    """Find all markdown files, respecting skip patterns."""
    cards = sorted(target_dir.rglob("*.md"))
    return [c for c in cards if not should_skip(c, config["skip_patterns"])]


def cmd_scan(target_dir: Path, config: dict, verbose: bool = False):
    """Analyze files without changes."""
    cards = find_cards(target_dir, config)
    with_yaml = 0
    without_yaml = 0
    has_relevance = 0
    has_tier = 0
    total_bytes = 0

    for card in cards:
        content = card.read_text(encoding="utf-8", errors="replace")
        total_bytes += len(content.encode("utf-8"))
        fields, body, had_yaml = parse_frontmatter(content)
        if had_yaml:
            with_yaml += 1
        else:
            without_yaml += 1
        if "relevance" in fields:
            has_relevance += 1
        if "tier" in fields:
            has_tier += 1
        if verbose and not had_yaml:
            print(f"  no yaml: {card.relative_to(target_dir)}")

    print(f"\n  scan results for {target_dir}:")
    print(f"    total files:     {len(cards)}")
    print(f"    with yaml:       {with_yaml}")
    print(f"    without yaml:    {without_yaml}")
    print(f"    has relevance:   {has_relevance}")
    print(f"    has tier:        {has_tier}")
    print(f"    total size:      {total_bytes / 1024:.0f} KB")
    print(f"    avg file size:   {total_bytes / max(len(cards), 1) / 1024:.1f} KB")

    if without_yaml:
        print(f"\n  → run 'init' to add YAML frontmatter to {without_yaml} files")
    if not has_relevance:
        print(f"  → run 'decay' to add relevance scores and tiers")


def cmd_init(target_dir: Path, config: dict, dry_run: bool = False, verbose: bool = False):
    """Add YAML frontmatter to files missing it."""
    cards = find_cards(target_dir, config)
    added = 0

    for card in cards:
        content = card.read_text(encoding="utf-8", errors="replace")
        fields, body, had_yaml = parse_frontmatter(content)
        if had_yaml:
            continue

        # Create minimal frontmatter
        new_fields = {
            "type": infer_type(card, config["type_inference"]),
        }
        title = infer_title(body)
        if title:
            new_fields["title"] = title

        ref_date = get_best_date({}, card, config["use_git_dates"])
        new_fields["last_accessed"] = ref_date.isoformat()
        days = max(0, (TODAY - ref_date).days)
        new_fields["content_tokens"] = str(estimate_tokens(body))
        new_fields["relevance"] = str(calc_relevance(days, config["decay_rate"], config["relevance_floor"]))
        new_fields["tier"] = calc_tier(days, config["tiers"])

        # Auto-extract l1_summary from ## Summary section if present
        l1 = extract_l1(body)
        if l1 and l1 != body.strip():
            sentences = re.split(r"(?<=[.!?])\s+", l1.strip())
            new_fields["l1_summary"] = " ".join(sentences[:2])

        new_content = build_frontmatter(new_fields) + body
        if not dry_run:
            card.write_text(new_content, encoding="utf-8")
        added += 1
        if verbose:
            print(f"  {'[dry] ' if dry_run else ''}init: {card.relative_to(target_dir)} → {new_fields['tier']}")

    print(f"\n  {'DRY RUN — ' if dry_run else ''}init results:")
    print(f"    files processed: {len(cards)}")
    print(f"    frontmatter added: {added}")


def cmd_decay(target_dir: Path, config: dict, dry_run: bool = False, verbose: bool = False):
    """Update relevance and tiers based on time decay."""
    cards = find_cards(target_dir, config)
    results = []

    for card in cards:
        content = card.read_text(encoding="utf-8", errors="replace")
        fields, body, had_yaml = parse_frontmatter(content)

        ref_date = get_best_date(fields, card, config["use_git_dates"])
        days = max(0, (TODAY - ref_date).days)

        old_tier = fields.get("tier", "")
        new_relevance = calc_relevance(days, config["decay_rate"], config["relevance_floor"])
        new_tier = calc_tier(days, config["tiers"], old_tier)

        if "last_accessed" not in fields:
            fields["last_accessed"] = ref_date.isoformat()
        if "type" not in fields:
            fields["type"] = infer_type(card, config["type_inference"])

        fields["content_tokens"] = str(estimate_tokens(body))
        fields["relevance"] = str(new_relevance)
        fields["tier"] = new_tier

        # Re-extract l1_summary from ## Summary if body has one and no cached summary
        if not fields.get("l1_summary"):
            l1 = extract_l1(body)
            if l1 and l1 != body.strip():
                sentences = re.split(r"(?<=[.!?])\s+", l1.strip())
                fields["l1_summary"] = " ".join(sentences[:2])

        new_content = build_frontmatter(fields) + body
        changed = new_content != content

        if changed and not dry_run:
            card.write_text(new_content, encoding="utf-8")

        results.append({
            "path": str(card.relative_to(target_dir)),
            "days": days,
            "relevance": new_relevance,
            "tier": new_tier,
            "changed": changed,
        })

        if verbose and changed:
            print(f"  {'[dry] ' if dry_run else ''}{card.relative_to(target_dir)}: {old_tier or '?'}→{new_tier} r={new_relevance}")

    # Stats
    tiers = {}
    changed_count = sum(1 for r in results if r["changed"])
    for r in results:
        tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1
    avg_rel = sum(r["relevance"] for r in results) / max(len(results), 1)

    print(f"\n  {'DRY RUN — ' if dry_run else ''}decay results:")
    print(f"    total: {len(results)}, changed: {changed_count}")
    print(f"    avg relevance: {avg_rel:.2f}")
    for tier in ["core", "active", "warm", "cold", "archive"]:
        count = tiers.get(tier, 0)
        bar = "█" * (count // 3)
        if count:
            print(f"    {tier:8s}: {count:4d} {bar}")


def cmd_touch(filepath: str, config: dict):
    """Promote a file one tier up (graduated recall).

    archive → cold → warm → active → active (refresh)
    Each touch promotes one level, not straight to top.
    Natural spaced repetition: multiple reads = stronger memory.
    """
    p = Path(filepath)
    if not p.exists():
        print(f"  error: {filepath} not found")
        sys.exit(1)

    content = p.read_text(encoding="utf-8", errors="replace")
    fields, body, had_yaml = parse_frontmatter(content)

    if fields.get("tier") == "core":
        fields["last_accessed"] = TODAY.isoformat()
        fields["relevance"] = "1.0"
        new_content = build_frontmatter(fields) + body
        p.write_text(new_content, encoding="utf-8")
        print(f"  touched: {filepath} → core (refreshed)")
        return

    tiers_cfg = config["tiers"]
    # Promotion targets: set last_accessed to midpoint of next-higher tier
    # archive → cold: midpoint of cold range
    # cold → warm: midpoint of warm range
    # warm → active: midpoint of active range
    # active → active: today (refresh)
    cold_threshold = tiers_cfg.get("cold", 60)
    warm_threshold = tiers_cfg.get("warm", 21)
    active_threshold = tiers_cfg.get("active", 7)

    current_tier = fields.get("tier", "archive")
    if current_tier == "archive":
        # Promote to cold: set last_accessed to midpoint of cold range
        target_days = (warm_threshold + cold_threshold) // 2
        new_tier = "cold"
    elif current_tier == "cold":
        # Promote to warm: midpoint of warm range
        target_days = (active_threshold + warm_threshold) // 2
        new_tier = "warm"
    elif current_tier == "warm":
        # Promote to active: midpoint of active range
        target_days = active_threshold // 2
        new_tier = "active"
    else:
        # Already active: refresh to today
        target_days = 0
        new_tier = "active"

    from datetime import timedelta
    new_date = TODAY - timedelta(days=target_days)
    new_relevance = calc_relevance(target_days, config["decay_rate"], config["relevance_floor"])

    fields["last_accessed"] = new_date.isoformat()
    fields["relevance"] = str(new_relevance)
    fields["tier"] = new_tier

    new_content = build_frontmatter(fields) + body
    p.write_text(new_content, encoding="utf-8")
    print(f"  touched: {filepath} → {current_tier}→{new_tier}, relevance={new_relevance}")


def cmd_creative(n: int, target_dir: Path, config: dict):
    """Random sample from cold/archive tiers for divergent thinking."""
    cards = find_cards(target_dir, config)
    cold_cards = []

    for card in cards:
        content = card.read_text(encoding="utf-8", errors="replace")
        fields, body, had_yaml = parse_frontmatter(content)
        tier = fields.get("tier", "")
        if tier in ("cold", "archive", "warm"):
            title = fields.get("title", "") or infer_title(body)
            cold_cards.append({
                "path": str(card.relative_to(target_dir)),
                "tier": tier,
                "relevance": fields.get("relevance", "?"),
                "title": title,
                "last_accessed": fields.get("last_accessed", "?"),
            })

    if not cold_cards:
        print("  no cold/archive cards found — memory is too fresh")
        return

    sample = random.sample(cold_cards, min(n, len(cold_cards)))
    print(f"\n  🎲 creative recall — {len(sample)} random cards:")
    for card in sample:
        print(f"    [{card['tier']}] {card['title'] or card['path']}")
        print(f"           {card['path']} (r={card['relevance']}, last={card['last_accessed']})")
    print(f"\n  read these cards and look for unexpected connections to your current task")


def cmd_daily(target_dir: Path, config: dict, dry_run: bool = False, verbose: bool = False):
    """Bootstrap and decay daily files (YYYY-MM-DD.md pattern)."""
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
    daily_files = sorted(
        f for f in target_dir.rglob("*.md")
        if date_pattern.match(f.name)
    )

    if not daily_files:
        print(f"  no daily files (YYYY-MM-DD.md) found in {target_dir}")
        return

    results = []
    for f in daily_files:
        content = f.read_text(encoding="utf-8", errors="replace")
        fields, body, had_yaml = parse_frontmatter(content)

        # Extract date from filename
        file_date = date.fromisoformat(f.stem)
        days = max(0, (TODAY - file_date).days)

        # Use file_date as reference (not git/mtime — daily files are date-intrinsic)
        la = fields.get("last_accessed", "")
        try:
            last_acc = date.fromisoformat(la[:10])
            # Use most recent of: file_date, last_accessed
            ref_days = max(0, (TODAY - max(file_date, last_acc)).days)
        except (ValueError, IndexError):
            ref_days = days

        new_relevance = calc_relevance(ref_days, config["decay_rate"], config["relevance_floor"])
        old_tier = fields.get("tier", "")
        new_tier = calc_tier(ref_days, config["tiers"], old_tier)

        fields["type"] = "daily"
        fields["date"] = file_date.isoformat()
        if "last_accessed" not in fields:
            fields["last_accessed"] = file_date.isoformat()
        fields["relevance"] = str(new_relevance)
        fields["tier"] = new_tier

        new_content = build_frontmatter(fields) + body
        changed = new_content != content

        if changed and not dry_run:
            f.write_text(new_content, encoding="utf-8")

        results.append({
            "file": f.name,
            "date": file_date.isoformat(),
            "days": ref_days,
            "relevance": new_relevance,
            "tier": new_tier,
            "changed": changed,
        })

        if verbose:
            print(f"  {'[dry] ' if dry_run else ''}{f.name}: {old_tier or '?'}→{new_tier} r={new_relevance} ({ref_days}d)")

    # Summary
    tiers = {}
    for r in results:
        tiers[r["tier"]] = tiers.get(r["tier"], 0) + 1
    changed_count = sum(1 for r in results if r["changed"])

    print(f"\n  {'DRY RUN — ' if dry_run else ''}daily results:")
    print(f"    files: {len(results)}, changed: {changed_count}")
    for tier in ["active", "warm", "cold", "archive"]:
        count = tiers.get(tier, 0)
        if count:
            dates = [r["date"] for r in results if r["tier"] == tier]
            print(f"    {tier:8s}: {count:3d}  ({dates[0]}..{dates[-1]})")


def cmd_search(query: str, target_dir: Path, config: dict, mode: str = "normal", level: int = 0):
    """Search cards by query, filtered by tier mode.

    Modes:
      heartbeat: core + active only
      normal:    core + active + warm (default)
      deep:      all tiers
      creative:  delegates to cmd_creative

    Levels (L0/L1/L2):
      0: description only (default)
      1: + l1_summary
      2: + full body
    """
    if mode == "creative":
        cmd_creative(5, target_dir, config)
        return

    tier_filter = {
        "heartbeat": {"core", "active"},
        "normal": {"core", "active", "warm"},
        "deep": {"core", "active", "warm", "cold", "archive"},
    }.get(mode, {"core", "active", "warm"})

    cards = find_cards(target_dir, config)
    query_lower = query.lower()
    results = []

    for card in cards:
        content = card.read_text(encoding="utf-8", errors="replace")
        fields, body, had_yaml = parse_frontmatter(content)
        tier = fields.get("tier", "unknown")
        if tier not in tier_filter:
            continue

        # Search in: description, tags, title, body
        searchable = " ".join([
            str(fields.get("description", "")),
            str(fields.get("tags", "")),
            fields.get("title", "") or infer_title(body),
            body,
        ]).lower()

        if query_lower in searchable:
            title = fields.get("title", "") or infer_title(body)
            result = {
                "path": str(card.relative_to(target_dir)),
                "tier": tier,
                "relevance": fields.get("relevance", "?"),
                "title": title,
                "description": fields.get("description", ""),
            }
            if level >= 1:
                result["l1_summary"] = extract_level(fields, body, 1)
            if level >= 2:
                result["body"] = body
            results.append(result)

    # Sort by relevance (highest first)
    results.sort(key=lambda r: float(r["relevance"]) if r["relevance"] != "?" else 0, reverse=True)

    # Limit results based on level to control output size
    max_results = {0: 20, 1: 5, 2: 3}.get(level, 20)
    display = results[:max_results]

    print(f"\n  search '{query}' (mode={mode}, level=L{level}) — {len(results)} results:")
    for r in display:
        desc = r["description"][:60] + "..." if len(r.get("description", "")) > 60 else r["description"]
        print(f"    [{r['tier']:7s}] {r['title'] or r['path']}")
        if desc:
            print(f"             {desc}")
        print(f"             {r['path']} (r={r['relevance']})")
        if level >= 1 and r.get("l1_summary"):
            summary = r["l1_summary"][:200] + "..." if len(r.get("l1_summary", "")) > 200 else r["l1_summary"]
            print(f"             L1: {summary}")
        if level >= 2 and r.get("body"):
            body_preview = r["body"][:500].replace("\n", "\n             ")
            print(f"             ─── full body ───")
            print(f"             {body_preview}")
            if len(r["body"]) > 500:
                print(f"             ... ({estimate_tokens(r['body'])} tokens total)")
    if len(results) > max_results:
        print(f"    ... and {len(results) - max_results} more")
    if not results:
        if mode != "deep":
            print(f"  → try --mode deep to search all tiers")


def cmd_promote(filepath: str, target_tier: str):
    """Manually set a card's tier (e.g., promote to core)."""
    valid_tiers = ("core", "active", "warm", "cold", "archive")
    if target_tier not in valid_tiers:
        print(f"  error: invalid tier '{target_tier}'. Must be one of: {', '.join(valid_tiers)}")
        sys.exit(1)

    p = Path(filepath)
    if not p.exists():
        print(f"  error: {filepath} not found")
        sys.exit(1)

    content = p.read_text(encoding="utf-8", errors="replace")
    fields, body, had_yaml = parse_frontmatter(content)

    old_tier = fields.get("tier", "unknown")
    fields["tier"] = target_tier

    # For core: set relevance to 1.0; for others: compute from tier midpoint
    if target_tier == "core":
        fields["relevance"] = "1.0"
        fields["last_accessed"] = TODAY.isoformat()
    elif target_tier == "active":
        fields["relevance"] = "1.0"
        fields["last_accessed"] = TODAY.isoformat()
    # For demoting, keep last_accessed as-is and let decay recalculate

    new_content = build_frontmatter(fields) + body
    p.write_text(new_content, encoding="utf-8")
    print(f"  {old_tier} → {target_tier}: {filepath}")


def cmd_stats(target_dir: Path, config: dict):
    """Show comprehensive memory health stats with context budget."""
    cards = find_cards(target_dir, config)
    tiers = {}
    tier_bytes = {}
    total_bytes = 0
    active_bytes = 0
    stale_count = 0
    no_yaml = 0
    l0_bytes = 0
    l1_bytes = 0
    l1_count = 0

    for card in cards:
        content = card.read_text(encoding="utf-8", errors="replace")
        content_bytes = len(content.encode("utf-8"))
        total_bytes += content_bytes
        fields, body, had_yaml = parse_frontmatter(content)
        if not had_yaml:
            no_yaml += 1
        tier = fields.get("tier", "unknown")
        tiers[tier] = tiers.get(tier, 0) + 1
        tier_bytes[tier] = tier_bytes.get(tier, 0) + content_bytes
        if tier in ("core", "active"):
            active_bytes += content_bytes
        # L0/L1 tracking
        desc = fields.get("description", "")
        l0_bytes += len(desc.encode("utf-8"))
        l1 = fields.get("l1_summary", "")
        if l1:
            l1_bytes += len(l1.encode("utf-8"))
            l1_count += 1
        try:
            la = date.fromisoformat(fields.get("last_accessed", "")[:10])
            if (TODAY - la).days > 90:
                stale_count += 1
        except (ValueError, IndexError):
            pass

    print(f"\n  memory health — {target_dir}")
    print(f"  {'─' * 40}")
    print(f"  total cards:       {len(cards)}")
    print(f"  total size:        {total_bytes / 1024:.0f} KB")
    print(f"  without yaml:      {no_yaml}")
    print(f"  stale (>90 days):  {stale_count}")
    print(f"  {'─' * 40}")
    print(f"  tier distribution:")
    for tier in ["core", "active", "warm", "cold", "archive", "unknown"]:
        count = tiers.get(tier, 0)
        if count:
            pct = count / len(cards) * 100
            bar = "█" * int(pct / 2)
            print(f"    {tier:8s}: {count:4d} ({pct:4.1f}%) {bar}")

    print(f"  {'─' * 40}")
    print(f"  active context:    {active_bytes / 1024:.0f} KB (~{active_bytes // 4:,} tokens)")
    print(f"  total context:     {total_bytes / 1024:.0f} KB (~{total_bytes // 4:,} tokens)")

    # Context budget
    budget = config.get("context_budget", DEFAULT_CONFIG["context_budget"])
    hot_limit = budget.get("hot_limit_kb", 4)
    active_limit = budget.get("active_limit_kb", 50)
    total_warn = budget.get("total_warn_kb", 500)

    active_kb = active_bytes / 1024
    total_kb = total_bytes / 1024

    print(f"  {'─' * 40}")
    print(f"  context budget:")

    def _budget_status(val_kb, limit_kb):
        return "[OK]" if val_kb <= limit_kb else "[OVER]"

    print(f"    active tier:   {active_kb:6.1f} KB / {active_limit} KB  {_budget_status(active_kb, active_limit)}  (~{active_bytes // 4:,} tokens)")
    print(f"    total vault:   {total_kb:6.1f} KB / {total_warn} KB  {_budget_status(total_kb, total_warn)}  (~{total_bytes // 4:,} tokens)")

    # L0/L1 index stats
    print(f"  {'─' * 40}")
    print(f"  hierarchical loading (L0/L1/L2):")
    print(f"    L0 index (descriptions): {l0_bytes / 1024:.1f} KB (~{l0_bytes // 4:,} tokens)")
    print(f"    L1 summaries:            {l1_count}/{len(cards)} cards ({l1_count * 100 // max(len(cards), 1)}%)")
    if l1_bytes:
        print(f"    L1 total if loaded:      {l1_bytes / 1024:.1f} KB (~{l1_bytes // 4:,} tokens)")

    # Warnings
    warnings = []
    if active_kb > active_limit:
        warnings.append(f"  WARNING: active tier ({active_kb:.0f} KB) exceeds budget ({active_limit} KB)")
        warnings.append(f"    → consider: compress, decay, or demote stale cards")
    if total_kb > total_warn:
        warnings.append(f"  WARNING: total vault ({total_kb:.0f} KB) exceeds budget ({total_warn} KB)")
        warnings.append(f"    → consider: compress --all")
    if l1_count < len(cards) // 2 and len(cards) > 10:
        warnings.append(f"  INFO: only {l1_count * 100 // max(len(cards), 1)}% cards have L1 summaries")
        warnings.append(f"    → run: generate-l1 to improve search efficiency")

    if warnings:
        print(f"  {'─' * 40}")
        for w in warnings:
            print(w)


def cmd_compress(target_dir: Path, config: dict, dry_run: bool = False,
                 verbose: bool = False, compress_all: bool = False):
    """Compress old daily files by extracting key information.

    Operates on cold/archive tier daily files with body > 5000 chars.
    With --all, also compresses non-daily archive cards > 10000 chars.
    """
    date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")
    cards = find_cards(target_dir, config)
    compressed = 0
    skipped = 0

    for card in cards:
        content = card.read_text(encoding="utf-8", errors="replace")
        fields, body, had_yaml = parse_frontmatter(content)
        tier = fields.get("tier", "")
        is_daily = date_pattern.match(card.name)

        # Determine if this card should be compressed
        if is_daily:
            if tier not in ("cold", "archive"):
                continue
            if len(body) < 5000:
                skipped += 1
                continue
        elif compress_all:
            if tier != "archive":
                continue
            if len(body) < 10000:
                skipped += 1
                continue
        else:
            continue

        # Skip already compressed cards
        if "### Key Decisions" in body or "### Entities Mentioned" in body:
            skipped += 1
            continue

        new_body = compress_body(body)

        # Update l1_summary from compressed body
        sentences = re.split(r"(?<=[.!?])\s+", new_body.strip())
        l1 = " ".join(sentences[:2]) if sentences else ""
        if l1:
            fields["l1_summary"] = l1
        fields["content_tokens"] = str(estimate_tokens(new_body))

        new_content = build_frontmatter(fields) + new_body
        if not dry_run:
            card.write_text(new_content, encoding="utf-8")
        compressed += 1
        if verbose:
            old_tokens = estimate_tokens(body)
            new_tokens = estimate_tokens(new_body)
            saved = old_tokens - new_tokens
            print(f"  {'[dry] ' if dry_run else ''}{card.relative_to(target_dir)}: {old_tokens}→{new_tokens} tokens (saved {saved})")

    print(f"\n  {'DRY RUN — ' if dry_run else ''}compress results:")
    print(f"    compressed: {compressed}")
    print(f"    skipped:    {skipped} (too small or already compressed)")


def cmd_generate_l1(target_dir: Path, config: dict, dry_run: bool = False, verbose: bool = False):
    """Generate L1 summaries for cards that don't have them."""
    cards = find_cards(target_dir, config)
    generated = 0
    already = 0
    updated_tokens = 0

    for card in cards:
        content = card.read_text(encoding="utf-8", errors="replace")
        fields, body, had_yaml = parse_frontmatter(content)
        if not had_yaml:
            continue

        changed = False

        # Generate l1_summary if missing
        if not fields.get("l1_summary") and body.strip():
            l1 = extract_l1(body)
            if l1 and l1 != body.strip():
                sentences = re.split(r"(?<=[.!?])\s+", l1.strip())
                fields["l1_summary"] = " ".join(sentences[:2])
                changed = True
                generated += 1
            elif len(body.split()) > 200:
                # No ## Summary section, use first 3 sentences of body
                sentences = re.split(r"(?<=[.!?])\s+", body.strip())
                if len(sentences) >= 2:
                    fields["l1_summary"] = " ".join(sentences[:3])
                    changed = True
                    generated += 1
        else:
            already += 1

        # Always update content_tokens
        tokens = str(estimate_tokens(body))
        if fields.get("content_tokens") != tokens:
            fields["content_tokens"] = tokens
            changed = True
            updated_tokens += 1

        if changed:
            new_content = build_frontmatter(fields) + body
            if not dry_run:
                card.write_text(new_content, encoding="utf-8")
            if verbose:
                summary = fields.get("l1_summary", "")[:80]
                print(f"  {'[dry] ' if dry_run else ''}{card.relative_to(target_dir)}: L1=\"{summary}...\"")

    print(f"\n  {'DRY RUN — ' if dry_run else ''}generate-l1 results:")
    print(f"    L1 generated:     {generated}")
    print(f"    already had L1:   {already}")
    print(f"    tokens updated:   {updated_tokens}")


# ─── main ───────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0)

    cmd = args[0]
    dry_run = "--dry-run" in args
    verbose = "--verbose" in args

    # Find config path
    config_path = None
    if "--config" in args:
        idx = args.index("--config")
        config_path = args[idx + 1] if idx + 1 < len(args) else None

    # Find target directory (first non-flag argument after command)
    target = None
    for a in args[1:]:
        if not a.startswith("-") and a != config_path:
            target = a
            break

    if cmd == "touch":
        if not target:
            print("  error: touch requires a file path")
            sys.exit(1)
        config = load_config(Path(target).parent, config_path)
        cmd_touch(target, config)
        return

    if cmd in ("promote", "demote"):
        # promote <file> <tier>  or  demote <file> <tier>
        positional = [a for a in args[1:] if not a.startswith("-") and a != config_path]
        if len(positional) < 2:
            print(f"  error: {cmd} requires <file> <tier>")
            print(f"  usage: {cmd} <filepath> core|active|warm|cold|archive")
            sys.exit(1)
        cmd_promote(positional[0], positional[1])
        return

    if cmd == "search":
        # search <query> [directory] [--mode heartbeat|normal|deep] [--level 0|1|2]
        mode = "normal"
        level = 0
        if "--mode" in args:
            idx = args.index("--mode")
            mode = args[idx + 1] if idx + 1 < len(args) else "normal"
        if "--level" in args:
            idx = args.index("--level")
            try:
                level = int(args[idx + 1]) if idx + 1 < len(args) else 0
            except ValueError:
                level = 0
        positional = [a for a in args[1:]
                      if not a.startswith("-") and a != config_path and a != mode and a != str(level)]
        if not positional:
            print("  error: search requires a query")
            print("  usage: search <query> [directory] [--mode heartbeat|normal|deep] [--level 0|1|2]")
            sys.exit(1)
        query = positional[0]
        search_dir = Path(positional[1]) if len(positional) > 1 else Path(".")
        config = load_config(search_dir, config_path)
        cmd_search(query, search_dir, config, mode, level)
        return

    if cmd == "creative":
        n = 5
        creative_dir = None
        for a in args[1:]:
            if not a.startswith("-"):
                try:
                    n = int(a)
                except ValueError:
                    creative_dir = a
        target_dir = Path(creative_dir) if creative_dir else Path(".")
        config = load_config(target_dir, config_path)
        cmd_creative(n, target_dir, config)
        return

    target_dir = Path(target) if target else Path(".")
    if not target_dir.is_dir():
        print(f"  error: {target_dir} is not a directory")
        sys.exit(1)

    config = load_config(target_dir, config_path)

    compress_all = "--all" in args

    if cmd == "scan":
        cmd_scan(target_dir, config, verbose)
    elif cmd == "init":
        cmd_init(target_dir, config, dry_run, verbose)
    elif cmd == "decay":
        cmd_decay(target_dir, config, dry_run, verbose)
    elif cmd == "daily":
        cmd_daily(target_dir, config, dry_run, verbose)
    elif cmd == "stats":
        cmd_stats(target_dir, config)
    elif cmd == "compress":
        cmd_compress(target_dir, config, dry_run, verbose, compress_all)
    elif cmd == "generate-l1":
        cmd_generate_l1(target_dir, config, dry_run, verbose)
    elif cmd == "config":
        save_default_config(target_dir)
    else:
        print(f"  unknown command: {cmd}")
        print("  commands: scan, init, decay, daily, touch, creative, search, promote, demote, stats, compress, generate-l1, config")
        sys.exit(1)


if __name__ == "__main__":
    main()
