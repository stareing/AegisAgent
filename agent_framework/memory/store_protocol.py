"""Re-export MemoryStoreProtocol for convenience.

The canonical definition lives in protocols/core.py.
This file exists to match the directory structure in section 18 of the architecture doc.
"""

from agent_framework.protocols.core import MemoryStoreProtocol

__all__ = ["MemoryStoreProtocol"]
