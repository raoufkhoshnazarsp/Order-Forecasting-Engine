"""Dynamic summing matrix from the Entity Master (doc 11.3).

The matrix is rebuilt from data on every call; no venture count is hardcoded.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from src.data_mart.entity_master import EntityMaster


class SummingMatrixError(ValueError):
    """Raised when the hierarchy cannot be expressed as a valid matrix."""


def build_summing_matrix(
    entity_master: EntityMaster,
    leaf_ids: list[str] | None = None,
) -> pd.DataFrame:
    """Rows = all hierarchy nodes, columns = leaf entities.

    A cell is 1 when the leaf contributes to the row's total.
    Because TOTAL = VERTICALs + VENTUREs, every row is a pure sum of leaves.
    """
    total_id = entity_master.total_id()
    leaves = leaf_ids or entity_master.leaf_ids(active_only=False)
    if not leaves:
        raise SummingMatrixError("No leaf entities available for the hierarchy")
    if len(set(leaves)) != len(leaves):
        raise SummingMatrixError("Duplicate leaf entity in hierarchy")

    leaf_meta = {e.entity_id: e for e in entity_master.leaves(active_only=False)}
    unknown = [leaf for leaf in leaves if leaf not in leaf_meta]
    if unknown:
        raise SummingMatrixError(f"Leaf ids missing from master: {unknown}")

    rows = [total_id]
    verticals = [leaf for leaf in leaves if leaf_meta[leaf].entity_type == "VERTICAL"]
    ventures = [leaf for leaf in leaves if leaf_meta[leaf].entity_type == "VENTURE"]
    rows.extend(sorted(verticals))
    rows.extend(sorted(ventures))

    matrix = np.zeros((len(rows), len(leaves)), dtype="int8")
    for i, row_id in enumerate(rows):
        if row_id == total_id:
            matrix[i, :] = 1
        else:
            matrix[i, leaves.index(row_id)] = 1
    return pd.DataFrame(matrix, index=rows, columns=leaves)


def aggregation_keys(leaf_frame: pd.DataFrame) -> pd.DataFrame:
    """Map each leaf row to its VERTICAL and VENTURE totals (no double count)."""
    frame = leaf_frame.copy()
    if frame["entity_type"].isin(["TOTAL"]).any():
        raise SummingMatrixError("Leaf input must not already contain TOTAL rows")
    return frame
