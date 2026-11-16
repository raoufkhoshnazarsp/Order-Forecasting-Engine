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
    as_of: object | None = None,
) -> pd.DataFrame:
    """Rows = all hierarchy nodes, columns = leaf entities.

    A cell is 1 when the leaf contributes to the row's total.
    Because TOTAL = VERTICALs + VENTUREs, every row is a pure sum of leaves.

    ``as_of`` restricts the leaves to those whose validity window contains that
    date. A venture that launched mid-history must not appear in a forecast
    made before it existed, and one that was retired must not reappear. Without
    ``as_of`` every leaf in the master is used, which is the right choice for a
    forecast at the current origin.
    """
    total_id = entity_master.total_id()
    leaves = leaf_ids or entity_master.leaf_ids(active_only=False)
    if as_of is not None:
        leaves = [leaf for leaf in leaves if _valid_at(entity_master, leaf, as_of)]
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


def _valid_at(entity_master: EntityMaster, entity_id: str, as_of: object) -> bool:
    """Whether a leaf's validity window contains ``as_of``.

    An empty ``valid_from`` means "since the beginning"; an empty ``valid_to``
    means "still open". Dates are compared as calendar dates so a timestamp
    does not accidentally fall outside its own window.
    """
    entity = entity_master.by_id(entity_id)
    if entity is None:
        return False
    day = pd.Timestamp(as_of).date()
    if entity.valid_from:
        if day < pd.Timestamp(entity.valid_from).date():
            return False
    if entity.valid_to:
        if day > pd.Timestamp(entity.valid_to).date():
            return False
    return True


def aggregation_keys(leaf_frame: pd.DataFrame) -> pd.DataFrame:
    """Map each leaf row to its VERTICAL and VENTURE totals (no double count)."""
    frame = leaf_frame.copy()
    if frame["entity_type"].isin(["TOTAL"]).any():
        raise SummingMatrixError("Leaf input must not already contain TOTAL rows")
    return frame
