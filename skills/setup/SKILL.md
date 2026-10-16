---
name: setup
description: First-time yoke setup: install the yoke CLI and check the environment. Run once before any other yoke skill, or again to update.
---

# setup

1. If `command -v yoke` fails, tell the user you will install the CLI from GitHub and ask to proceed. Then run `uv tool install --force git+https://github.com/bharath-gp/agents-yoke`; if `uv` is missing use `pipx install --force git+https://github.com/bharath-gp/agents-yoke`; if neither exists, ask the user to install `uv` (https://docs.astral.sh/uv) and stop. If `yoke` is already present, skip this step unless the user asked to update (then run the same command).
2. Ask the user whether to set up for all their projects (`user`) or only the current repo (`project`); default `user`. Run `yoke setup --scope <choice>`. It lists the tools found (claude, agy, git required; gh, ollama optional) and exits 1 if a required one is missing: report which and how to install, then stop.
3. If installing the CLI was step 1, tell the user to restart the agent session so the `yoke` MCP server connects.
4. Tell the user setup is done and the next step is `/yoke:grill`.
