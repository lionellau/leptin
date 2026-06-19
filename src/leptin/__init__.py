"""Leptin — the satiety hormone for agent memory.

A local-first *control loop* for agent memory. It rides your coding agent's
harness hooks (session-start, post-tool, pre-compact) to keep long-term memory
correct and useful over time: resolving contradictions so the current truth
wins, capturing mistakes into never-decaying lessons, learning which memories
actually help, and running a recall guardrail that proves nothing useful was
silently forgotten. The lean MCP surface (``recall``/``remember``) is just the
part the model needs in-band.

The core runs fully offline on the Python standard library. Hosted embeddings
and LLM-powered merging are optional upgrades (see ``leptin.embeddings`` and
``leptin.llm``).
"""

from leptin.config import Config
from leptin.engine import DietEngine
from leptin.storage import Store

__version__ = "1.2.0"

__all__ = ["Config", "DietEngine", "Store", "__version__"]
