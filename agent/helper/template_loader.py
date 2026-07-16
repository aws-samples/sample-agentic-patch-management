"""Response templates loaded on-demand via tool, not baked into system prompts."""

import os
import yaml
import logging
from strands import tool

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


def get_template_names_for_agent(agent_name: str) -> str:
    """Get a one-line summary of available templates for the system prompt."""
    templates = _load_templates()
    if not templates:
        return ""

    names = [key for key, tmpl in templates.items() if isinstance(tmpl, dict)]
    if not names:
        return ""

    return f"\n\nRESPONSE FORMATTING: Call get_response_template(name) before writing your final answer. Available: {', '.join(names)}"


@tool
def get_response_template(template_name: str, execution_id: str = "") -> str:
    """Get a response template for formatting your final answer.

    Decision: For operation_initiated template, you MUST pass the execution_id
    from the tool result. If no execution_id was returned (gate or error), do
    NOT call this template — present the tool result directly instead.

    Args:
        template_name: Template name (from the available list in your instructions)
        execution_id: Required for operation_initiated — the automation_execution_id
                      or scan_command_id from the tool that started the operation.

    Returns: Template structure text, or error if not found/invalid.
    """
    templates = _load_templates()
    tmpl = templates.get(template_name)
    if not tmpl or not isinstance(tmpl, dict):
        available = [k for k, v in templates.items() if isinstance(v, dict)]
        return f"Unknown template: {template_name}. Available: {', '.join(available)}"

    # Layer 2: Guard — operation_initiated requires a real execution ID.
    # This prevents the model from formatting a success message when the
    # tool returned a gate/error instead of actually starting an operation.
    if template_name == 'operation_initiated':
        if not execution_id or execution_id in ('PENDING', 'None', 'null', ''):
            return (
                "ERROR: Cannot use operation_initiated template without a valid execution_id. "
                "The operation may not have started. Check the tool result — if it has "
                "result_type='gate_blocked' or result_type='error', present THAT to the "
                "operator instead. Do NOT claim the operation is running."
            )

    desc = tmpl.get('description', template_name)
    structure = tmpl.get('structure', '').rstrip()
    return f"[{template_name.upper()}] ({desc})\n{structure}"
