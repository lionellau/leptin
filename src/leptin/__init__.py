"""Leptin — the satiety hormone for agent memory.

A drop-in MCP memory server that puts an agent's long-term memory on a token
budget, shows an auditable ledger of tokens & dollars saved, and runs a recall
guardrail that proves the diet never silently forgot anything you needed.

The core runs fully offline on the Python standard library. Hosted embeddings
and LLM-powered merging are optional upgrades (see ``leptin.embeddings`` and
``leptin.llm``).
"""

from leptin.config import Config
from leptin.engine import DietEngine
from leptin.storage import Store

__version__ = "1.0.0"

__all__ = ["Config", "DietEngine", "Store", "__version__"]
