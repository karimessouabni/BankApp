from datetime import date

from dateutil.relativedelta import relativedelta
from pydantic import BaseModel, model_validator

MAX_RETENTION_YEARS = 5

DAYS = "days"
YEARS = "years"
_UNITS = (DAYS, YEARS)
_KEYS = ("default", "minimum", "maximum")


def years_to_days(years: int, start: date | None = None) -> int:
    """Équivalent exact en jours de `years` années à partir de `start` (bissextiles comprises)."""
    start = start or date.today()
    return (start + relativedelta(years=years) - start).days


def max_retention_days(start: date | None = None) -> int:
    """Nombre exact de jours dans 5 ans à partir de `start` (bissextiles comprises).

    1826 ou 1827 selon le nombre de 29 février dans la fenêtre. Calculé à la
    date de la demande : la même saisie en jours peut donc être acceptée un
    jour et refusée un autre, à un jour près.
    """
    return years_to_days(MAX_RETENTION_YEARS, start)


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
    @model_validator(mode="before")
    @classmethod
    def _auto_enable_retention(cls, values):
        if not isinstance(values, dict):
            return values
        retention_flag_given = "retention_enabled" in values
        any_retention_param = any(
            values.get(f"{k}_{unit}") is not None
            for k in _KEYS
            for unit in _UNITS
        )
        if not retention_flag_given and any_retention_param:
            values["retention_enabled"] = True
        return values

    # ── 2. Tous les contrôles du payload, erreurs accumulées ─────────────
    # Un seul validateur "after" qui n'échoue qu'à la fin : le client reçoit
    # la liste complète des problèmes de son payload en une seule réponse.
    @model_validator(mode="after")
    def _validate(self) -> "BucketRetention":
        errors = []

        used = [unit for unit in _UNITS
                if any(getattr(self, f"{k}_{unit}") is not None for k in _KEYS)]
        if len(used) > 1:
            errors.append(
                "Retention must be set either in days (…_days) or in years (…_years) "
                "for all three attributes, not a mix of both."
            )

        for k in _KEYS:
            for unit in _UNITS:
                v = getattr(self, f"{k}_{unit}")
                if v is not None and v <= 0:
                    errors.append(f"{k}_{unit} must be superior to 0.")

        # Bornes et plafond : seulement quand l'unité est non ambiguë.
        if len(used) == 1:
            unit = used[0]
            limit = max_retention(unit)
            mn, df, mx = self.minimum, self.default, self.maximum

            for name, v in (("minimum", mn), ("default", df), ("maximum", mx)):
                if v is not None and v > limit:
                    errors.append(
                        f"{name}_{unit} ({v} {unit}) cannot be superior to "
                        f"{MAX_RETENTION_YEARS} years ({limit} {unit}, leap years included)."
                    )
            if mn is not None and mx is not None and mn > mx:
                errors.append("Retention minimum cannot be superior to maximum.")
            if df is not None:
                if mn is not None and df < mn:
                    errors.append("Retention default cannot be inferior to minimum.")
                if mx is not None and df > mx:
                    errors.append("Retention default cannot be superior to maximum.")

        if errors:
            raise ValueError(" | ".join(errors))
        return self

    # ── Lecture : l'unité saisie et les valeurs brutes ───────────────────
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

    def in_days(self, key: str) -> int | None:
        """Valeur de l'attribut normalisée en jours, quelle que soit l'unité saisie."""
        value = self._value(key)
        if value is None:
            return None
        return value if self.unit == DAYS else years_to_days(value)

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
