"""Load response templates and inject into agent prompts."""

import os
import yaml
import logging

logger = logging.getLogger(__name__)

_templates_cache = None


def _load_templates() -> dict:
    """Load templates from YAML file (cached)."""
    global _templates_cache
    if _templates_cache is not None:
        return _templates_cache

    config_path = os.path.join(
        os.path.dirname(os.path.dirname(__file__)), 'config', 'response_templates.yaml'
    )
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            _templates_cache = yaml.safe_load(f) or {}
        logger.info(f"Loaded {len(_templates_cache)} response templates")
    except Exception as e:
        logger.warning(f"Could not load response templates: {e}")
        _templates_cache = {}
    return _templates_cache


def get_templates_for_agent(agent_name: str) -> str:
    """
    Get formatted template block for a specific agent.

    Returns a string to append to the agent's system prompt.
    Only includes templates tagged for that agent.

    Agent name mapping:
      - vulnerability_analyst / vulnerability-analyst
      - patch_manager / patch-manager
      - compliance_analyst / compliance-analyst
      - supervisor / patch-automation-supervisor
    """
    templates = _load_templates()
    if not templates:
        return ""

    # Normalize agent name for matching
    normalized = agent_name.replace("-", "_").replace("patch_automation_", "")

    matching = []
    for key, tmpl in templates.items():
        if not isinstance(tmpl, dict):
            continue
        tmpl_agent = tmpl.get('agent', '').replace("-", "_")
        if tmpl_agent == normalized:
            matching.append((key, tmpl))

    if not matching:
        return ""

    lines = ["\n\nRESPONSE TEMPLATES — use the matching template for your response type:"]
    for key, tmpl in matching:
        desc = tmpl.get('description', key)
        structure = tmpl.get('structure', '').rstrip()
        lines.append(f"\n[{key.upper()}] ({desc})")
        lines.append(structure)

    return "\n".join(lines)
