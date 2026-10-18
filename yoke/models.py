"""Model discovery and static fallbacks for yoke (PLAN 2.6)."""
import subprocess
from typing import Dict, List, Optional, Tuple

CLAUDE_MODELS = ("opus", "sonnet", "haiku")
STATIC_AGY_MODELS = (
    "gemini-3.8-flash-high",
    "gemini-3.8-flash-low",
    "claude-sonnet-5-5-high",
    "claude-opus-5-5-high",
)


def list_models(agent) -> Tuple[List[Dict[str, str]], Optional[str]]:
    """Return (models, warning) for an agent. Never raises."""
    agent_name = getattr(agent, "name", agent)
    if not isinstance(agent_name, str):
        return [], "warning: invalid agent %r" % (agent,)
    agent_name = agent_name.lower().strip()

    if agent_name == "claude":
        return [{"id": m, "label": ""} for m in CLAUDE_MODELS], None

    if agent_name == "agy":
        static_list = [{"id": m, "label": ""} for m in STATIC_AGY_MODELS]
        try:
            proc = subprocess.run(
                ["agy", "models"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode != 0:
                err = (proc.stderr or proc.stdout or ("exit %d" % proc.returncode)).strip()
                return static_list, "warning: 'agy models' failed (%s); using static list" % err
            models = []
            for line in proc.stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                if "\t" in line:
                    mid, label = line.split("\t", 1)
                    models.append({"id": mid.strip(), "label": label.strip()})
                else:
                    models.append({"id": line, "label": ""})
            if not models:
                return static_list, "warning: 'agy models' returned no models; using static list"
            return models, None
        except Exception as e:
            return static_list, "warning: 'agy models' failed (%s); using static list" % e

    return [], "warning: unknown agent %r" % agent
