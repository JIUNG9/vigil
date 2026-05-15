"""MTTD layer — three-tier detection.

Layer 1 (rule):       50ms — SigNoz alert webhook → match watchlist rule → fire
Layer 2 (similarity): 500ms — embed current symptom → Qdrant search past INCDs
Layer 3 (pattern):    optional, deferred — LLM-learned precursors (high false-positive risk)

This module exposes Layer 1 + Layer 2. Layer 3 lives behind a feature flag.
"""

from teammate.mttd.rule_layer import RuleLayer
from teammate.mttd.similarity_layer import SimilarityLayer

__all__ = ["RuleLayer", "SimilarityLayer"]
