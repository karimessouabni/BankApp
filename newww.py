from datetime import date

from dateutil.relativedelta import relativedelta

# Mêmes constantes et helpers que bucket_retention.py — à importer depuis le
# modèle une fois le snippet recollé dans le vrai projet.
MAX_RETENTION_YEARS = 5

DAYS = "days"
YEARS = "years"


def max_retention_days(start: date | None = None) -> int:
    """Nombre exact de jours dans 5 ans à partir de `start` (bissextiles comprises)."""
    start = start or date.today()
    return (start + relativedelta(years=MAX_RETENTION_YEARS) - start).days


def max_retention(unit) -> int:
    """Plafond dans l'unité saisie : 5 années, ou leur équivalent exact en jours."""
    return MAX_RETENTION_YEARS if unit == YEARS else max_retention_days()

_RETENTION_KEYS = ("default", "minimum", "maximum")
_OBJECT_LOCK_LABEL = "Object lock retention"
_OBJECT_LOCK_FIELDS = ("object_lock_duration_days", "object_lock_duration_years")


# ── Briques communes create / update ─────────────────────────────────────
def format_limit(unit) -> str:
    """Plafond lisible dans l'unité saisie : "5 years" ou "5 years (1826 days)"."""
    if unit == YEARS:
        return f"{MAX_RETENTION_YEARS} years"
    return f"{MAX_RETENTION_YEARS} years ({max_retention_days()} days)"


def check_single_unit(days, years, label, days_field, years_field, errors) -> str | None:
    """Vérifie qu'une seule unité est saisie et renvoie laquelle ("days"/"years"/None)."""
    if days is not None and years is not None:
        logging.info(f"check_single_unit {label} both")
        errors.append(
            f"{label} must be set either in days ({days_field}) "
            f"or in years ({years_field}), not both."
        )
        return None
    if days is not None:
        return DAYS
    if years is not None:
        return YEARS
    return None


def check_duration_bounds(unit, duration, label, errors) -> None:
    """Durée > 0 et ≤ 5 ans, comparée dans l'unité saisie."""
    if duration <= 0:
        logging.info(f"check_duration_bounds {label} zero")
        errors.append(f"{label} ({duration} {unit}) cannot be inferior or equal to ZERO.")

    if duration > max_retention(unit):
        logging.info(f"check_duration_bounds {label} limit")
        errors.append(
            f"{label} ({duration} {unit}) cannot be superior to {format_limit(unit)}."
        )


def resolve_object_lock(days, years, errors):
    """(unité, durée) de l'object lock, validées. (None, None) si rien n'est saisi."""
    unit = check_single_unit(days, years, _OBJECT_LOCK_LABEL, *_OBJECT_LOCK_FIELDS, errors)
    if unit is None:
        return None, None

    duration = days if unit == DAYS else years
    check_duration_bounds(unit, duration, _OBJECT_LOCK_LABEL, errors)
    return unit, duration


def write_object_lock(immutability, unit, duration) -> None:
    """Écrit l'object lock dans l'unité saisie ; l'autre unité reste à None."""
    immutability["object_locking_enabled"] = True
    immutability["object_versioning_enabled"] = True
    immutability["object_lock_duration_days"] = duration if unit == DAYS else None
    immutability["object_lock_duration_years"] = duration if unit == YEARS else None


def check_retention_bounds(unit, default, minimum, maximum, errors) -> None:
    """Applique min ≤ default ≤ max et le plafond 5 ans, dans l'unité saisie."""
    if minimum <= 0 or minimum > default:
        logging.info("check_retention_bounds1")
        errors.append(
            f"Retention minimum ({minimum} {unit}) cannot be inferior or equal to 0 "
            f"nor superior to default ({default} {unit})."
        )

    if default < minimum or default >= maximum:
        logging.info("check_retention_bounds2")
        errors.append(
            f"Retention default ({default} {unit}) cannot be inferior to minimum "
            f"({minimum} {unit}) nor superior or equal to maximum ({maximum} {unit})."
        )

    if maximum > max_retention(unit) or maximum <= default:
        logging.info("check_retention_bounds3")
        errors.append(
            f"Retention maximum ({maximum} {unit}) cannot be inferior or equal to default "
            f"({default} {unit}) nor superior to {format_limit(unit)}."
        )


def raise_on_errors(errors) -> None:
    if errors:
        global_message = " | ".join(errors)
        raise DeclineDemandException(global_message)


# ── CREATE ───────────────────────────────────────────────────────────────
def compute_bucket_object_lock(
    immutability,
    object_lock_duration_days,
    object_lock_duration_years,
):
    errors = []

    unit, duration = resolve_object_lock(
        object_lock_duration_days,
        object_lock_duration_years,
        errors,
    )

    if unit is None and not errors:
        logging.info("compute_bucket_object_lock1")
        errors.append(
            f"{_OBJECT_LOCK_LABEL} must be set, either in days "
            f"({_OBJECT_LOCK_FIELDS[0]}) or in years ({_OBJECT_LOCK_FIELDS[1]})."
        )

    raise_on_errors(errors)

    write_object_lock(immutability, unit, duration)
    logging.info(f"compute_bucket_object_lock2 {immutability}")
    return immutability


def compute_bucket_retention(retention: BucketRetention, immutability: dict) -> dict:
    """Valide la rétention saisie et la reporte dans immutability["retention"].

    Le modèle garantit déjà une unité unique ; ici on exige les trois attributs
    et on applique min ≤ default ≤ max ainsi que le plafond de 5 ans, dans
    l'unité saisie. Aucune conversion : les champs du modèle sont recopiés tels
    quels, l'unité non utilisée reste à None.
    """
    unit = retention.unit
    default, minimum, maximum = retention.default, retention.minimum, retention.maximum

    if None in (unit, default, minimum, maximum):
        logging.info("compute_bucket_retention1")
        raise DeclineDemandException(
            "Retention configuration is not valid. You must set default, minimum and maximum "
            "in the same unit, either in days (…_days) or in years (…_years)."
        )

    errors = []
    check_retention_bounds(unit, default, minimum, maximum, errors)
    raise_on_errors(errors)

    immutability["retention"]["retention_enabled"] = retention.retention_enabled
    for key in _RETENTION_KEYS:
        for u in (DAYS, YEARS):
            immutability["retention"][f"{key}_{u}"] = getattr(retention, f"{key}_{u}")

    logging.info(f"compute_bucket_retention2 {immutability}")
    return immutability


# ── UPDATE ───────────────────────────────────────────────────────────────
def validate_object_lock_update(
    existing_immutability: dict,
    object_lock_duration_days,
    object_lock_duration_years,
    errors,
) -> None:
    """Object lock côté update : la saisie remplace l'existant, sinon on le garde."""
    if object_lock_duration_days is None and object_lock_duration_years is None:
        logging.info("validate_immutability4")
        # Rien de resaisi : on reprend l'existant dans son unité d'origine.
        object_lock_duration_days = existing_immutability.get("object_lock_duration_days")
        object_lock_duration_years = existing_immutability.get("object_lock_duration_years")

    unit, duration = resolve_object_lock(
        object_lock_duration_days,
        object_lock_duration_years,
        errors,
    )

    # unit None sans erreur = pas d'object lock sur ce bucket, rien à valider.
    if unit is not None:
        write_object_lock(existing_immutability, unit, duration)
        logging.info(f"validate_immutability5 : {existing_immutability}")


def validate_retention_update(
    retention,
    bucket: dict,
    existing_immutability: dict,
    errors,
) -> None:
    """Rétention côté update : saisie complète, ou complétée depuis la base à unité égale."""
    existing_retention = existing_immutability["retention"]
    # Les buckets créés avant l'ajout des années sont stockés en jours.
    existing_unit = existing_retention.get("unit") or DAYS

    if not retention or retention.is_empty() or not retention.retention_enabled:
        logging.info("validate_immutability6")
        unit = existing_unit
        values = {key: existing_retention[key] for key in _RETENTION_KEYS}
    else:
        unit = retention.unit
        values = {key: getattr(retention, f"{key}_{unit}") for key in _RETENTION_KEYS}
        missing = [key for key in _RETENTION_KEYS if values[key] is None]
        if missing and unit == existing_unit:
            # attribut non resaisi : on garde la valeur déjà en base, même unité
            for key in missing:
                values[key] = bucket[f"retention_{key}"]
        elif missing:
            logging.info("validate_immutability7")
            errors.append(
                f"Retention is currently set in {existing_unit}. Switching to {unit} requires "
                f"default, minimum and maximum to be submitted together."
            )
            return

    default, minimum, maximum = values["default"], values["minimum"], values["maximum"]

    if default is None or minimum is None or maximum is None:
        logging.info("validate_immutability8")
        errors.append(
            "Retention configuration is not valid. You must set default, minimum and maximum "
            "in the same unit, either in days or in years."
        )
        return

    check_retention_bounds(unit, default, minimum, maximum, errors)

    existing_immutability["retention"] = {
        "retention_enabled": True,
        "unit": unit,
        "default": default,
        "minimum": minimum,
        "maximum": maximum,
    }
    logging.info(f"validate_immutability9 : {existing_immutability}")


def validate_immutability(
    retention,
    backup,
    bucket: dict,
    existing_immutability: dict,
    object_lock_duration_days=None,
    object_lock_duration_years=None,
) -> dict:
    errors = []

    if backup and not backup.is_empty() and backup.backup_enabled:
        errors.append(
            "Retention and bucket backup are not compatible. "
            "We cannot activate backup when retention is enabled."
        )

    validate_object_lock_update(
        existing_immutability,
        object_lock_duration_days,
        object_lock_duration_years,
        errors,
    )

    validate_retention_update(retention, bucket, existing_immutability, errors)

    raise_on_errors(errors)
    return existing_immutability
