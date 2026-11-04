"""Move a tool's display block out of the model's context and onto the UI.

Tools may return {"display": {"type": ..., ...}} alongside their data. The
model only needs the data; the display block is what the user sees as a card.
CardCollector strips it from the tool result before the model (and memory)
receive it, and queues it for the runtime to stream as a card event.
"""

import json
import logging
from typing import Any, List, Optional, Tuple

from strands.hooks import AfterToolCallEvent, HookProvider, HookRegistry

logger = logging.getLogger(__name__)

MAX_CARD_BYTES = 24_000
# Read cards: the reply under them should add judgement, not repeat the data.
CARD_REPLY_HINT = ("The operator sees this result as a card. Reply in two or three sentences on what "
                   "matters and what to do next. No table, and don't list what the card shows.")
_OWN_HINT_TYPES = ("plan", "run")


class CardCollector(HookProvider):
    """Strip display blocks from tool results and queue them as cards."""

    def __init__(self) -> None:
        self._pending: List[dict] = []

    def register_hooks(self, registry: HookRegistry, **kwargs: Any) -> None:
        registry.add_callback(AfterToolCallEvent, self._after_tool)

    def drain(self) -> List[dict]:
        """Return and clear the cards collected so far."""
        cards, self._pending = self._pending, []
        return cards

    def extract(self, data: dict) -> Tuple[dict, Optional[dict]]:
        """Split a tool result dict into (data for the model, card or None).

        Args:
            data: Decoded tool result.

        Returns:
            The result without its display block, and the card if it is usable.
        """
        display = data.get("display")
        if "display" not in data:
            return data, None
        rest = {k: v for k, v in data.items() if k != "display"}
        if not isinstance(display, dict) or not isinstance(display.get("type"), str):
            return rest, None
        size = len(json.dumps(display, default=str))
        if size > MAX_CARD_BYTES:
            logger.warning(f"[CARD] dropped {display['type']} card ({size} bytes)")
            return rest, None
        rest["shown_as_card"] = True
        if display["type"] not in _OWN_HINT_TYPES:
            rest["next_action"] = f"{CARD_REPLY_HINT} {rest.get('next_action') or ''}".strip()
        return rest, display

    def _after_tool(self, event: AfterToolCallEvent) -> None:
        result = event.result
        if not isinstance(result, dict):
            return
        content = result.get("content") or []
        changed = False
        new_content = []
        for block in content:
            data = None
            if isinstance(block, dict) and isinstance(block.get("json"), dict):
                data, key = block["json"], "json"
            elif isinstance(block, dict) and isinstance(block.get("text"), str) and block["text"].lstrip().startswith("{"):
                try:
                    data, key = json.loads(block["text"]), "text"
                except json.JSONDecodeError:
                    data = None
            if not isinstance(data, dict) or "display" not in data:
                new_content.append(block)
                continue
            rest, card = self.extract(data)
            if card is not None:
                self._pending.append(card)
            new_content.append({"json": rest} if key == "json" else {"text": json.dumps(rest, default=str)})
            changed = True
        if changed:
            event.result = {**result, "content": new_content}
