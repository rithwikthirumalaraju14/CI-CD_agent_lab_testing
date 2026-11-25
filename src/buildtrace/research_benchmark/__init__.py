"""OPERATOR ONLY: deterministic local project snapshots and trusted verification.

Copy only materialize_case(...).destination to an agent-visible case branch.
Do not expose this package, its catalogue, or the separate verifier bundle.
"""

from .catalogue import CASES, catalogue, get_case, validate_catalogue
from .materialize import materialize_baseline, materialize_case, reference_edits, snapshot_files
from .model import AnchorError, MaterializedCase, VerificationContract, VerificationResult
from .verify import load_contract, verify_snapshot

__all__ = [
    "AnchorError", "CASES", "MaterializedCase", "VerificationContract", "VerificationResult",
    "catalogue", "get_case", "load_contract", "materialize_baseline", "materialize_case",
    "reference_edits", "snapshot_files", "validate_catalogue", "verify_snapshot",
]
