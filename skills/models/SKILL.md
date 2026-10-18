---
name: models
description: Configure, view, or reset model choices and reasoning effort for primary and secondary agents across plan and cheap tiers.
---

# models

Gate: run `yoke setup --check` first. If it fails or `yoke` is not found, stop and tell the user to run `/yoke:setup`.

Configure, inspect, or reset model choices and reasoning effort per agent across two tiers: `plan` and `cheap`.

1. Inspect current and available models:
   - Run `yoke models --show` to display currently configured models and effort for each agent and tier.
   - Run `yoke models --list` to discover available models for each agent (`claude` static choices, `agy models` dynamic choices).
2. Ask the user what they want to configure:
   - To update models: ask the user to pick plan-tier and cheap-tier models for primary and secondary agents (`claude` and `agy`). Present current settings from `yoke models --show` as defaults.
   - To reset models: if the user wants to revert to defaults, run `yoke models --reset` (optionally specifying `AGENT` or `AGENT.TIER`, e.g. `yoke models --reset claude.plan`).
3. Apply changes:
   - Run `yoke models --set AGENT.TIER=MODEL` (e.g., `yoke models --set claude.plan=opus --set agy.cheap=gemini-3.8-flash-low`). Repeat `--set` for multiple tiers/agents.
   - If specifying reasoning effort, pass `--effort <level>` (e.g. low, medium, high) alongside `--set` (applies to the whole `--set` invocation).
   - Use `--scope project` (or `repo`) to save in `<repo>/.yoke.json` or `--scope user` to save in `~/.yoke/config.json` (user and project are the usual choices; default: user).
4. Verify by running `yoke models --show` and summarize the effective configuration to the user.
