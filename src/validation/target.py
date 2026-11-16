"""Target Definition: versioned business rule and its enforcement.

The engine must refuse to run a production extraction while the Target
Definition is still a draft (unapproved fields hold ``None``). This is the
guard that prevents an ambiguous target silently reaching the data mart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

APPROVED_STATUSES = {"DRAFT", "APPROVED"}

# Fields that must be explicitly decided by the business before production use.
REQUIRED_DECISIONS = ("exclude_deleted", "date_field", "reversal_policy")

ALLOWED_DATE_FIELDS = ("registered_date", "created_date")

ALLOWED_REVERSAL_POLICIES = ("keep", "exclude")

# Statuses that mean an order was reversed or refunded. Used only when the
# business chose ``reversal_policy: exclude``.
DEFAULT_REVERSAL_STATUSES = ("REVERSED", "REFUNDED", "REFUND", "REVERSAL")

# Column whitelist: identifiers interpolated into SQL must come from here.
ALLOWED_COLUMNS = frozenset(
    {
        "id",
        "venture_id",
        "wallet_id",
        "status",
        "merchant_type",
        "payment_method_type",
        "registered_at",
        "registered_date",
        "created_at",
        "created_date",
        "amount",
        "discount_amount",
        "voucher_discount_amount",
        "deleted_at",
    }
)

SQL_KEYWORDS = frozenset(
    {"AND", "OR", "NOT", "IS", "NULL", "IN", "TRUE", "FALSE", "ALL"}
)


class TargetDefinitionError(RuntimeError):
    """Raised when the Target Definition is incomplete or unsafe."""


@dataclass(frozen=True)
class TargetDefinition:
    version: str
    status: str
    order_status: str
    merchant_type: str | None
    exclude_deleted: bool | None
    date_field: str | None
    payment_methods: Any
    reversal_policy: str | None
    organic_field: str = "voucher_discount_amount"
    reversal_statuses: tuple[str, ...] = DEFAULT_REVERSAL_STATUSES

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "TargetDefinition":
        td = config.get("target_definition", config)
        missing = [
            key
            for key in ("version", "definition_status", "order_status")
            if td.get(key) is None
        ]
        if missing:
            raise TargetDefinitionError(f"Target Definition missing keys: {missing}")

        status = str(td["definition_status"]).upper()
        if status not in APPROVED_STATUSES:
            raise TargetDefinitionError(f"Unknown Target Definition status: {status}")

        date_field = td.get("date_field")
        if date_field is not None and date_field not in ALLOWED_DATE_FIELDS:
            raise TargetDefinitionError(
                f"date_field {date_field!r} is not an approved column"
            )

        reversal_policy = td.get("reversal_policy")
        if (
            reversal_policy is not None
            and str(reversal_policy).lower() not in ALLOWED_REVERSAL_POLICIES
        ):
            raise TargetDefinitionError(
                f"reversal_policy {reversal_policy!r} must be one of "
                f"{ALLOWED_REVERSAL_POLICIES}"
            )
        reversal_statuses = tuple(
            str(s).upper()
            for s in (td.get("reversal_statuses") or DEFAULT_REVERSAL_STATUSES)
        )

        organic = td.get("organic_rule") or {}
        organic_field = organic.get("field", "voucher_discount_amount")
        if organic_field not in ALLOWED_COLUMNS:
            raise TargetDefinitionError(f"Unapproved organic field: {organic_field!r}")

        return cls(
            version=str(td["version"]),
            status=status,
            order_status=str(td["order_status"]),
            merchant_type=td.get("merchant_type"),
            exclude_deleted=td.get("exclude_deleted"),
            date_field=date_field,
            payment_methods=td.get("payment_methods", "all"),
            reversal_policy=(
                str(reversal_policy).lower() if reversal_policy is not None else None
            ),
            organic_field=organic_field,
            reversal_statuses=reversal_statuses,
        )

    # -- guards ---------------------------------------------------------
    def unresolved_decisions(self) -> list[str]:
        return [name for name in REQUIRED_DECISIONS if getattr(self, name) is None]

    def is_production_ready(self) -> bool:
        return self.status == "APPROVED" and not self.unresolved_decisions()

    def assert_production_ready(self) -> None:
        if self.status != "APPROVED":
            raise TargetDefinitionError(
                f"Target Definition {self.version} is {self.status}; "
                "business approval is required before production extraction."
            )
        unresolved = self.unresolved_decisions()
        if unresolved:
            raise TargetDefinitionError(
                "Unresolved Target Definition decisions: " + ", ".join(unresolved)
            )

    # -- SQL rendering --------------------------------------------------
    def date_field_for_draft(self) -> str:
        """Fallback date column for discovery runs on a draft definition."""
        return self.date_field or "registered_date"

    def status_predicate(self, alias: str = "O") -> str:
        return f"{alias}.status = '{self.order_status}'"

    def scope_predicates(self, alias: str = "O") -> list[str]:
        """Approved target-level filters only. Scope exclusions are separate."""
        predicates: list[str] = []
        if self.merchant_type:
            predicates.append(f"{alias}.merchant_type = '{self.merchant_type}'")
        if self.exclude_deleted is True:
            predicates.append(f"{alias}.deleted_at IS NULL")
        if self.reversal_policy == "exclude" and self.reversal_statuses:
            values = ", ".join(f"'{s}'" for s in self.reversal_statuses)
            predicates.append(f"{alias}.status NOT IN ({values})")
        if isinstance(self.payment_methods, Sequence) and not isinstance(
            self.payment_methods, str
        ):
            values = ", ".join(f"'{str(v)}'" for v in self.payment_methods)
            predicates.append(f"{alias}.payment_method_type IN ({values})")
        return predicates


def render_vertical_list(verticals: Sequence[str]) -> str:
    if not verticals:
        raise TargetDefinitionError("scope.verticals must not be empty")
    return ", ".join("'" + str(v).replace("'", "''") + "'" for v in verticals)
