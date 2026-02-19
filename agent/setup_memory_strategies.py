#!/usr/bin/env python3
"""One-time setup: Add long-term memory strategies to the existing memory resource.

Run this ONCE after deployment to enable semantic + summary memory extraction.
After running, the memory resource will asynchronously extract long-term memories
from new conversation events.

Usage:
    python setup_memory_strategies.py

Requires:
    - AWS credentials configured (same profile as agent deployment)
    - bedrock_agentcore SDK installed
"""

import os
import sys
import yaml
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)


def load_memory_id() -> str:
    """Load memory_id from .bedrock_agentcore.yaml"""
    config_path = os.path.join(os.path.dirname(__file__), '.bedrock_agentcore.yaml')
    with open(config_path) as f:
        config = yaml.safe_load(f)
    default_agent = config.get('default_agent', '')
    memory_id = config['agents'][default_agent]['memory']['memory_id']
    logger.info(f"Memory ID: {memory_id}")
    return memory_id


def main():
    from bedrock_agentcore.memory import MemoryClient

    memory_id = load_memory_id()
    client = MemoryClient()

    # Check current strategies
    existing = client.get_memory_strategies(memory_id)
    existing_types = {s.get('type', s.get('memoryStrategyType', '')) for s in existing}
    logger.info(f"Existing strategies: {existing_types or 'none (STM_ONLY)'}")

    added = []

    # Namespace pattern — must use {memoryStrategyId} (not {strategyId})
    # API pattern: [a-zA-Z0-9\-_\/]*(\{(actorId|sessionId|memoryStrategyId)\}...)*
    NS = "/actor/{actorId}/strategy/{memoryStrategyId}/{sessionId}"

    # Add semantic strategy — extracts facts/knowledge from conversations
    # e.g., "CVE-2024-6387 required rollback on 2 prod instances"
    if 'SEMANTIC' not in existing_types:
        logger.info("Adding SEMANTIC strategy...")
        client.add_semantic_strategy(
            memory_id=memory_id,
            name="patch_knowledge",
            description="Extracts patching facts: CVE outcomes, instance behaviors, rollback history, environment quirks",
            namespaces=[NS]
        )
        added.append('SEMANTIC')
        logger.info("✓ SEMANTIC strategy added")
    else:
        logger.info("SEMANTIC strategy already exists, skipping")

    # Add summary strategy — running conversation summary per session
    # Gives agents a compressed view of what happened in previous sessions
    if 'SUMMARIZATION' not in existing_types:
        logger.info("Adding SUMMARY strategy...")
        client.add_summary_strategy(
            memory_id=memory_id,
            name="session_summary",
            description="Summarizes patching sessions: what was scanned, patched, rolled back, and outcomes",
            namespaces=[NS]
        )
        added.append('SUMMARY')
        logger.info("✓ SUMMARY strategy added")
    else:
        logger.info("SUMMARY strategy already exists, skipping")

    if added:
        logger.info(f"Done. Added strategies: {', '.join(added)}")
        logger.info("Long-term memories will be extracted from NEW conversation events going forward.")
        logger.info("Namespace pattern: /actor/{actorId}/strategy/{strategyId}/{sessionId}")
        logger.info("This means memories are automatically scoped per operator (actorId).")
    else:
        logger.info("No changes needed — all strategies already configured.")

    # Verify final state
    final = client.get_memory_strategies(memory_id)
    logger.info(f"Final strategies ({len(final)}):")
    for s in final:
        sid = s.get('strategyId', s.get('memoryStrategyId', '?'))
        stype = s.get('type', s.get('memoryStrategyType', '?'))
        logger.info(f"  - {stype}: {sid}")


if __name__ == '__main__':
    main()
