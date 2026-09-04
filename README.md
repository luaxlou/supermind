# Supermind

Superpowers gives agents skills. Supermind decides what to do next.

Supermind is a product-development brain for Codex. Describe the result you want; Supermind reads the current product and codebase, chooses the next action, calls the right Superpowers skill when needed, and keeps moving until the result is complete.

## What it does

- Starts a new product with one valuable end-to-end use case.
- Adds features without rebuilding the whole product story.
- Changes behavior while keeping affected features and contracts aligned.
- Fixes defects, improves measurable quality, refactors, releases, and extracts reusable capabilities.
- Uses Brainstorming only when a real product decision remains unresolved.
- Brings you in only when different choices would create meaningfully different product outcomes.

## Use it

You do not choose a workflow. State your intent:

```text
Use Supermind to add repeat ordering to this product.
```

Supermind decides whether to work directly or call Brainstorming, planning, debugging, execution, review, verification, or branch-finishing capabilities from Superpowers.

## Install

Install and enable the Superpowers Plugin first. Then add this public marketplace and install Supermind:

```bash
codex plugin marketplace add luaxlou/supermind --ref main
codex plugin add supermind@supermind
```

Start a new Codex task after installation, then invoke `$supermind` or ask Codex to use Supermind.

## Project structure

```text
.agents/plugins/marketplace.json   Public marketplace entry
plugins/supermind/                 Codex Plugin
  .codex-plugin/plugin.json        Plugin manifest
  skills/supermind/                Supermind Skill
scripts/verify.sh                  Local release checks
```

