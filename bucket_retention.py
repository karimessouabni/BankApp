from typing import Optional
from pydantic import BaseModel, root_validator

DAYS_PER_YEAR = 365
COS_MAX_RETENTION_DAYS = 365_243   # limite IBM COS (1000 ans)
_KEYS = ("default", "minimum", "maximum")


class BucketRetention(BaseModel):
    retention_enabled: bool = False
    default_days: int | None = None
    default_years: int | None = None
    minimum_days: int | None = None
    minimum_years: int | None = None
    maximum_days: int | None = None
    maximum_years: int | None = None

    # ── helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _to_days(days: int | None, years: int | None) -> int | None:
        if days is not None:
            return days
        if years is not None:
            return years * DAYS_PER_YEAR
        return None

    # ── 1. Auto-enable si un paramètre est fourni sans le flag ───────────
    @root_validator(pre=True)
    def _auto_enable_retention(cls, values: dict) -> dict:
        retention_flag_given = "retention_enabled" in values
        any_retention_param = any(
            values.get(f"{k}_{unit}") is not None
            for k in _KEYS
            for unit in ("days", "years")
        )
        if not retention_flag_given and any_retention_param:
            values["retention_enabled"] = True
        return values

    # ── 2. Exclusivité days / years + valeurs positives ──────────────────
    @root_validator(pre=True)
    def _days_xor_years(cls, values: dict) -> dict:
        for k in _KEYS:
            d, y = values.get(f"{k}_days"), values.get(f"{k}_years")
            if d is not None and y is not None:
                raise ValueError(f"{k}: fournir {k}_days OU {k}_years, pas les deux")
            for name, v in ((f"{k}_days", d), (f"{k}_years", y)):
                if v is not None and int(v) <= 0:
                    raise ValueError(f"{name} doit être > 0")
        return values

    # ── 3. Cohérence min ≤ default ≤ max et plafond COS ──────────────────
    @root_validator(skip_on_failure=True)
    def _check_bounds(cls, values: dict) -> dict:
        mn = cls._to_days(values.get("minimum_days"), values.get("minimum_years"))
        df = cls._to_days(values.get("default_days"), values.get("default_years"))
        mx = cls._to_days(values.get("maximum_days"), values.get("maximum_years"))

        for name, v in (("minimum", mn), ("default", df), ("maximum", mx)):
            if v is not None and v > COS_MAX_RETENTION_DAYS:
                raise ValueError(f"{name} dépasse {COS_MAX_RETENTION_DAYS} jours (limite COS)")
        if mn is not None and mx is not None and mn > mx:
            raise ValueError("minimum > maximum")
        if df is not None:
            if mn is not None and df < mn:
                raise ValueError("default < minimum")
            if mx is not None and df > mx:
                raise ValueError("default > maximum")
        return values

    # ── Propriétés normalisées en jours (compat avec le reste du code) ───
    @property
    def default(self) -> int | None:
        return self._to_days(self.default_days, self.default_years)

    @property
    def minimum(self) -> int | None:
        return self._to_days(self.minimum_days, self.minimum_years)

    @property
    def maximum(self) -> int | None:
        return self._to_days(self.maximum_days, self.maximum_years)

    def __iter__(self):
        yield "default", self.default
        yield "minimum", self.minimum
        yield "maximum", self.maximum

    def is_empty(self) -> bool:
        return self.default is None and self.minimum is None and self.maximum is None

    def to_cos_payload(self) -> dict:
        """Dict prêt pour le body PUT ?protection / retention_rule Terraform (en jours)."""
        return {k: v for k, v in self if v is not None}
