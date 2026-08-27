# 1 an = 365 jours par convention (comme l'object lock S3/COS) : le plafond est
# une constante, pas un calendrier. La fenêtre de rétention démarre à l'écriture
# de chaque objet, pas à la création du bucket, donc il n'existe pas de "nombre
# exact de jours pour 5 ans" à la date de la demande.
# Mêmes constantes que bucket_retention.py — à importer depuis le modèle
# une fois le snippet recollé dans le vrai projet.
DAYS_PER_YEAR = 365
MAX_RETENTION_YEARS = 5
MAX_RETENTION_DAYS = MAX_RETENTION_YEARS * DAYS_PER_YEAR  # 1825

DAYS = "days"
YEARS = "years"
MAX_RETENTION = {DAYS: MAX_RETENTION_DAYS, YEARS: MAX_RETENTION_YEARS}

_RETENTION_KEYS = ("default", "minimum", "maximum")


def format_limit(unit) -> str:
    """Plafond lisible dans l'unité saisie : "5 years" ou "5 years (1825 days)"."""
    if unit == YEARS:
        return f"{MAX_RETENTION_YEARS} years"
    return f"{MAX_RETENTION_YEARS} years ({MAX_RETENTION_DAYS} days)"


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


def check_retention_bounds(unit, default, minimum, maximum, errors) -> None:
    """Applique min ≤ default ≤ max et le plafond 5 ans, dans l'unité saisie."""
    limit = MAX_RETENTION[unit]

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

    if maximum > limit or maximum <= default:
        logging.info("check_retention_bounds3")
        errors.append(
            f"Retention maximum ({maximum} {unit}) cannot be inferior or equal to default "
            f"({default} {unit}) nor superior to {format_limit(unit)}."
        )


def compute_bucket_object_lock(
    immutability,
    object_lock_duration_days,
    object_lock_duration_years,
):
    errors = []

    unit = check_single_unit(
        object_lock_duration_days,
        object_lock_duration_years,
        "Object lock retention",
        "object_lock_duration_days",
        "object_lock_duration_years",
        errors,
    )

    if unit is None and not errors:
        logging.info("compute_bucket_object_lock1")
        errors.append(
            "Object lock retention must be set, either in days "
            "(object_lock_duration_days) or in years (object_lock_duration_years)."
        )

    if unit is not None:
        duration = object_lock_duration_days if unit == DAYS else object_lock_duration_years
        limit = MAX_RETENTION[unit]

        if duration <= 0:
            logging.info("compute_bucket_object_lock2")
            errors.append(
                f"Object lock retention ({duration} {unit}) cannot be inferior or equal to ZERO."
            )

        if duration > limit:
            logging.info("compute_bucket_object_lock3")
            errors.append(
                f"Object lock retention ({duration} {unit}) cannot be superior to "
                f"{format_limit(unit)}."
            )

    if errors:
        global_message = " | ".join(errors)
        raise DeclineDemandException(global_message)

    immutability["object_locking_enabled"] = True
    immutability["object_versioning_enabled"] = True
    # L'unité saisie est conservée telle quelle : l'autre reste à None.
    immutability["object_lock_duration_days"] = object_lock_duration_days
    immutability["object_lock_duration_years"] = object_lock_duration_years

    logging.info(f"compute_bucket_object_lock4 {immutability}")
    return immutability


def compute_bucket_retention(retention: BucketRetention, immutability: dict, operation: str) -> dict:
    errors = []

    unit = retention.unit
    values = {key: getattr(retention, f"{key}_{unit}") if unit else None
              for key in _RETENTION_KEYS}
    default, minimum, maximum = values["default"], values["minimum"], values["maximum"]

    if default is None or minimum is None or maximum is None:
        logging.info("compute_bucket_retention1")
        errors.append(
            "Retention configuration is not valid. You must set default, minimum and maximum "
            "in the same unit, either in days (…_days) or in years (…_years)."
        )
    else:
        check_retention_bounds(unit, default, minimum, maximum, errors)

    if errors:
        global_message = " | ".join(errors)
        raise DeclineDemandException(global_message)

    # Aucune conversion : seule l'unité saisie est renseignée, l'autre reste à None.
    immutability["retention"]["retention_enabled"] = retention.retention_enabled
    for key in _RETENTION_KEYS:
        immutability["retention"][f"{key}_days"] = values[key] if unit == DAYS else None
        immutability["retention"][f"{key}_years"] = values[key] if unit == YEARS else None

    logging.info(f"compute_bucket_retention2 {immutability} for operation {operation}")
    return immutability


def validate_immutability(retention, backup, bucket: dict, existing_immutability: dict) -> dict:
    errors = []

    if backup and not backup.is_empty() and backup.backup_enabled:
        errors.append(
            "Retention and bucket backup are not compatible. "
            "We cannot activate backup when retention is enabled."
        )

    existing_retention = existing_immutability["retention"]
    # Les buckets créés avant l'ajout des années sont stockés en jours.
    existing_unit = existing_retention.get("unit") or DAYS

    if not retention or retention.is_empty() or not retention.retention_enabled:
        logging.info("validate_immutability9")
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
            logging.info("validate_immutability9a")
            errors.append(
                f"Retention is currently set in {existing_unit}. Switching to {unit} requires "
                f"default, minimum and maximum to be submitted together."
            )

    default, minimum, maximum = values["default"], values["minimum"], values["maximum"]
    incomplete = default is None or minimum is None or maximum is None

    if incomplete and not errors:
        logging.info("validate_immutability10")
        errors.append(
            "Retention configuration is not valid. You must set default, minimum and maximum "
            "in the same unit, either in days or in years."
        )
    elif not incomplete:
        check_retention_bounds(unit, default, minimum, maximum, errors)

        existing_immutability["retention"] = {
            "retention_enabled": True,
            "unit": unit,
            "default": default,
            "minimum": minimum,
            "maximum": maximum,
        }
        logging.info(f"validate_immutability11 : {existing_immutability}")

    if errors:
        global_message = " | ".join(errors)
        raise DeclineDemandException(global_message)

    return existing_immutability
