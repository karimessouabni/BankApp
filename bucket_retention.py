from datetime import date

from dateutil.relativedelta import relativedelta
from pydantic import BaseModel, root_validator

MAX_RETENTION_YEARS = 5

DAYS = "days"
YEARS = "years"
_UNITS = (DAYS, YEARS)
_KEYS = ("default", "minimum", "maximum")


def max_retention_days(start: date | None = None) -> int:
    """Nombre exact de jours dans 5 ans à partir de `start` (bissextiles comprises).

    1826 ou 1827 selon le nombre de 29 février dans la fenêtre. Calculé à la
    date de la demande : la même saisie en jours peut donc être acceptée un
    jour et refusée un autre, à un jour près.
    """
    start = start or date.today()
    return (start + relativedelta(years=MAX_RETENTION_YEARS) - start).days


def max_retention(unit: str) -> int:
    """Plafond dans l'unité saisie : 5 années, ou leur équivalent exact en jours."""
    return MAX_RETENTION_YEARS if unit == YEARS else max_retention_days()


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
    def _unit_of(get) -> str | None:
        """Unité effectivement saisie ("days", "years") ou None si rien n'est saisi."""
        for unit in _UNITS:
            if any(get(f"{k}_{unit}") is not None for k in _KEYS):
                return unit
        return None

    # ── 1. Auto-enable si un paramètre est fourni sans le flag ───────────
    @root_validator(pre=True)
    def _auto_enable_retention(cls, values: dict) -> dict:
        retention_flag_given = "retention_enabled" in values
        any_retention_param = any(
            values.get(f"{k}_{unit}") is not None
            for k in _KEYS
            for unit in _UNITS
        )
        if not retention_flag_given and any_retention_param:
            values["retention_enabled"] = True
        return values

    # ── 2. Une seule unité pour tout le bloc + valeurs positives ─────────
    @root_validator(pre=True)
    def _single_unit(cls, values: dict) -> dict:
        used = [unit for unit in _UNITS
                if any(values.get(f"{k}_{unit}") is not None for k in _KEYS)]
        if len(used) > 1:
            raise ValueError(
                "retention: saisir les trois attributs en jours (…_days) OU en années "
                "(…_years), jamais un mélange des deux"
            )
        for k in _KEYS:
            for unit in _UNITS:
                v = values.get(f"{k}_{unit}")
                if v is not None and int(v) <= 0:
                    raise ValueError(f"{k}_{unit} doit être > 0")
        return values

    # ── 3. Cohérence min ≤ default ≤ max et plafond 5 ans ────────────────
    @root_validator(skip_on_failure=True)
    def _check_bounds(cls, values: dict) -> dict:
        unit = cls._unit_of(values.get)
        if unit is None:
            return values

        limit = max_retention(unit)
        mn = values.get(f"minimum_{unit}")
        df = values.get(f"default_{unit}")
        mx = values.get(f"maximum_{unit}")

        for name, v in (("minimum", mn), ("default", df), ("maximum", mx)):
            if v is not None and v > limit:
                raise ValueError(
                    f"{name}_{unit} dépasse {limit} {unit} "
                    f"({MAX_RETENTION_YEARS} ans maximum, années bissextiles comprises)"
                )
        if mn is not None and mx is not None and mn > mx:
            raise ValueError("minimum > maximum")
        if df is not None:
            if mn is not None and df < mn:
                raise ValueError("default < minimum")
            if mx is not None and df > mx:
                raise ValueError("default > maximum")
        return values

    # ── Lecture : l'unité saisie et les valeurs brutes, sans conversion ──
    @property
    def unit(self) -> str | None:
        return self._unit_of(lambda name: getattr(self, name))

    def _value(self, key: str) -> int | None:
        unit = self.unit
        return getattr(self, f"{key}_{unit}") if unit else None

    @property
    def default(self) -> int | None:
        return self._value("default")

    @property
    def minimum(self) -> int | None:
        return self._value("minimum")

    @property
    def maximum(self) -> int | None:
        return self._value("maximum")

    @property
    def max_allowed(self) -> int | None:
        """Plafond applicable dans l'unité saisie : 5 années, ou leur équivalent en jours."""
        unit = self.unit
        return max_retention(unit) if unit else None

    def __iter__(self):
        yield "unit", self.unit
        yield "default", self.default
        yield "minimum", self.minimum
        yield "maximum", self.maximum

    def is_empty(self) -> bool:
        return self.unit is None

    def to_cos_payload(self) -> dict:
        """Dict prêt pour le retention_rule Terraform, dans l'unité saisie par le client."""
        if self.is_empty():
            return {}
        return {k: v for k, v in self if v is not None}
