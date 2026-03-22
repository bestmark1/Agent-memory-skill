# Agent Memory

Memory management system for AI agents with Ebbinghaus-inspired forgetting curve, hierarchical loading (L0/L1/L2), and automatic session compression.

Organizes markdown files into a searchable vault with YAML frontmatter, automatic relevance decay, tiered recall (core / active / warm / cold / archive), creative random recall, and context budget awareness.

## Why

AI agents forget everything between sessions. Common workarounds — dumping everything into context or vector-searching everything — create bloat and noise. Human memory works differently: it has layers, it decays, and it surprises you with random connections.

Agent Memory brings this to AI agents:
- **Cards decay over time** — unused knowledge fades naturally, no manual cleanup
- **Tiered search** — quick checks hit only hot cards; deep analysis searches everything
- **Hierarchical loading** — L0/L1/L2 levels inspired by [OpenViking](https://github.com/volcengine/OpenViking) save tokens
- **Session compression** — old daily files are auto-compressed preserving decisions and action items
- **Creative recall** — random surfacing of forgotten cards for unexpected connections
- **Context budget** — track token usage per tier with warnings when budgets are exceeded
- **Zero dependencies** — works with plain markdown files, optional PyYAML for advanced YAML

## Quick Start

```bash
# 1. Scan your markdown directory
python3 scripts/memory-engine.py scan vault/

# 2. Bootstrap YAML frontmatter on existing files
python3 scripts/memory-engine.py init vault/

# 3. Run decay (schedule daily via cron)
python3 scripts/memory-engine.py decay vault/

# 4. Search cards filtered by tier
python3 scripts/memory-engine.py search "acme" vault/ --mode normal

# 5. Search with L1 summaries for quick triage
python3 scripts/memory-engine.py search "acme" vault/ --level 1

# 6. Generate L1 summaries for all cards
python3 scripts/memory-engine.py generate-l1 vault/

# 7. Compress old daily files
python3 scripts/memory-engine.py compress vault/ --verbose

# 8. Check memory health and context budget
python3 scripts/memory-engine.py stats vault/
```

## Commands

| Command | Description |
|---------|-------------|
| `scan [dir]` | Analyze files, report stats (no changes) |
| `init [dir]` | Add YAML frontmatter to files missing it |
| `decay [dir]` | Update relevance scores and tiers |
| `touch <file>` | Mark card as recently accessed (graduated +1 tier) |
| `search <query> [dir]` | Search cards by text, filtered by tier |
| `promote <file> <tier>` | Manually set card tier (e.g., to core) |
| `demote <file> <tier>` | Manually set card tier (e.g., to archive) |
| `creative <N> [dir]` | Random N cards from cold/archive for brainstorming |
| `compress [dir]` | Compress old daily files (cold/archive), extracting key decisions and entities |
| `generate-l1 [dir]` | Generate cached L1 summaries and token counts from card bodies |
| `stats [dir]` | Show tier distribution, context budget, and L0/L1 coverage |
| `config [dir]` | Generate default `.memory-config.json` |

Options: `--dry-run`, `--verbose`, `--config <path>`, `--mode heartbeat|normal|deep`, `--level 0|1|2`, `--all`

## How It Works

### Forgetting Curve

Each card has `relevance: 0.0-1.0` that decays linearly:

```
Day 0:   1.00 → tier: active
Day 7:   0.90 → tier: active
Day 21:  0.69 → tier: warm
Day 33:  0.50 → tier: cold
Day 60+: 0.10 → tier: archive (floor)
```

`core` tier is manual-only — for identity, security, pricing. Never auto-demoted.

### Tiered Search

| Mode | Tiers Searched | Use Case |
|------|---------------|----------|
| heartbeat | core + active | Quick checks, monitoring |
| normal | core + active + warm | Most questions (default) |
| deep | all tiers | Strategy, complex analysis |
| creative | random cold + archive | Brainstorming, ideation |

### Three-Layer Architecture

| Layer | What | Size Target | Loaded |
|-------|------|-------------|--------|
| Hot context | State file (volatile focus, blockers) | < 4 KB | Every turn |
| Searchable vault | Cards with YAML, one per entity | Unlimited | On demand |
| Archive | Old logs, completed work | Unlimited | Deep/creative only |

### Hierarchical Loading (L0/L1/L2)

Inspired by [OpenViking](https://github.com/volcengine/OpenViking)'s three-tier context loading, cards support progressive disclosure to save tokens:

| Level | Content | Tokens | Use Case |
|-------|---------|--------|----------|
| L0 | `description` field only | ~100 | Quick identification, search results |
| L1 | `l1_summary` or `## Summary` section | ~500 | Triage, decision-making |
| L2 | Full card body | Unlimited | Deep reading |

Use `--level 0|1|2` with `search` to control loading depth. Run `generate-l1` to pre-generate L1 summaries.

### Session Compression

The `compress` command auto-compresses old daily files (cold/archive tiers, >5000 chars):
1. Extracts key decisions (keywords: "decided", "agreed", "resolved"...)
2. Extracts entities (@mentions, $amounts, capitalized names)
3. Extracts action items (checkboxes, TODOs)
4. Replaces body with structured compressed version

Use `--all` to also compress non-daily archive cards (>10000 chars).

### Context Budget

The `stats` command tracks token usage per tier and warns when budgets are exceeded:

```
context budget:
  active tier:    42 KB / 50 KB  [OK]   (~10,500 tokens)
  total vault:   380 KB / 500 KB [OK]   (~95,000 tokens)

hierarchical loading (L0/L1/L2):
  L0 index:      2.1 KB (~525 tokens)
  L1 coverage:   45/120 cards (37%)
```

## YAML Frontmatter

Minimum (auto-managed by engine):
```yaml
---
relevance: 0.85
last_accessed: 2026-02-25
tier: active
---
```

New auto-managed fields for hierarchical loading:
```yaml
l1_summary: "Core client info: FMCG, $66K deal, negotiation phase"
content_tokens: 847
```

Full schema with domain-specific fields in [`references/yaml-schema.md`](references/yaml-schema.md).

## Configuration

`.memory-config.json` in target directory:

```json
{
  "tiers": {"active": 7, "warm": 21, "cold": 60},
  "decay_rate": 0.015,
  "relevance_floor": 0.1,
  "skip_patterns": ["_index.md"],
  "type_inference": {"crm/": "crm", "leads/": "lead"},
  "use_git_dates": true,
  "context_budget": {
    "hot_limit_kb": 4,
    "active_limit_kb": 50,
    "total_warn_kb": 500
  }
}
```

Adjust thresholds for your domain: tight for sales (3/10/30), wide for research (14/45/120).

## As a Claude Code Skill

Copy to your skills directory and use via `/agent-memory`:

```bash
cp -r . ~/.claude/skills/agent-memory/
```

The skill provides guided workflows for setup, maintenance, health checks, compression, and creative recall.

## Tests

```bash
python3 scripts/test_memory_engine.py
```

39 tests covering: YAML parsing roundtrips, relevance/tier calculations, init/decay with dry-run, search filtering, promote/demote, L0/L1/L2 extraction, compression, generate-l1, context budget warnings, backward compatibility.

## License

MIT
