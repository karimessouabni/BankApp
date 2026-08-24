DAYS_PER_YEAR = 365  # MAX_RETENTION_DAYS = 5 * 365 = 1825


def compute_total_object_lock_days(days, years) -> int:
    """Convertit la saisie utilisateur (jours/années) en total de jours."""
    return (days or 0) + (years or 0) * DAYS_PER_YEAR


def compute_bucket_object_lock(
    immutability,
    object_lock_duration_days,
    object_lock_duration_years,
):
    errors = []

    total_days = compute_total_object_lock_days(
        object_lock_duration_days,
        object_lock_duration_years,
    )

    if (object_lock_duration_days or 0) < 0 or (object_lock_duration_years or 0) < 0:
        logging.info("compute_bucket_object_lock0")
        errors.append("Object lock retention values cannot be negative.")

    if total_days <= 0:
        logging.info("compute_bucket_object_lock1")
        errors.append("Object lock retention days minimum cannot be inferior or equal to ZERO.")

    if total_days > MAX_RETENTION_DAYS:
        logging.info("compute_bucket_object_lock2")
        errors.append(
            f"Object lock total retention ({total_days} days) cannot be superior to 5 years "
            f"({MAX_RETENTION_DAYS} days)."
        )

    if errors:
        global_message = " | ".join(errors)
        raise DeclineDemandException(global_message)

    immutability["object_locking_enabled"] = True
    immutability["object_versioning_enabled"] = True
    immutability["object_lock_duration_days"] = total_days

    logging.info(f"compute_bucket_object_lock3 {immutability}")
    return immutability


_RETENTION_KEYS = ("default", "minimum", "maximum")


def compute_retention_total_days(days, years):
    """Total de jours pour un attribut de rétention, ou None si rien n'est saisi."""
    if days is None and years is None:
        return None
    return compute_total_object_lock_days(days, years)


def compute_bucket_retention(retention: BucketRetention, immutability: dict, operation: str) -> dict:
    errors = []

    totals = {
        key: compute_retention_total_days(
            getattr(retention, f"{key}_days"),
            getattr(retention, f"{key}_years"),
        )
        for key in _RETENTION_KEYS
    }
    default, minimum, maximum = totals["default"], totals["minimum"], totals["maximum"]

    if default is None or minimum is None or maximum is None:
        logging.info("compute_bucket_retention1")
        raise DeclineDemandException(
            "Retention configuration is not valid. You must set all attributes "
            "(<attribute>_days and/or <attribute>_years) to update the existing bucket retention."
        )

    if minimum <= 0 or minimum > default:
        logging.info("compute_bucket_retention2")
        errors.append(
            f"Retention minimum ({minimum} days) cannot be inferior or equal to 0 "
            f"nor superior to default ({default} days)."
        )

    if default < minimum or default >= maximum:
        logging.info("compute_bucket_retention3")
        errors.append(
            f"Retention default ({default} days) cannot be inferior to minimum ({minimum} days) "
            f"nor superior or equal to maximum ({maximum} days)."
        )

    if maximum > MAX_RETENTION_DAYS or maximum <= default:
        logging.info("compute_bucket_retention4")
        errors.append(
            f"Retention maximum ({maximum} days) cannot be inferior or equal to default "
            f"({default} days) nor superior to 5 years ({MAX_RETENTION_DAYS} days)."
        )

    if errors:
        global_message = " | ".join(errors)
        raise DeclineDemandException(global_message)

    # Les trois attributs sont normalisés en jours, comme object_lock_duration_days.
    immutability["retention"]["retention_enabled"] = retention.retention_enabled
    for key in _RETENTION_KEYS:
        immutability["retention"][f"{key}_days"] = totals[key]
        immutability["retention"][f"{key}_years"] = None

    logging.info(f"compute_bucket_retention5 {immutability} for operation {operation}")
    return immutability


def validate_immutability(retention, backup, bucket: dict, existing_immutability: dict) -> dict:
    errors = []

    if backup and not backup.is_empty() and backup.backup_enabled:
        errors.append(
            "Retention and bucket backup are not compatible. "
            "We cannot activate backup when retention is enabled."
        )

    if not retention or retention.is_empty() or not retention.retention_enabled:
        logging.info("validate_immutability9")
        # existing_immutability ne porte pas de jours/années : ses trois attributs
        # viennent des colonnes retention_* du bucket, déjà exprimées en jours.
        existing_retention = existing_immutability["retention"]
        totals = {key: existing_retention[key] for key in _RETENTION_KEYS}
    else:
        totals = {
            key: compute_retention_total_days(
                getattr(retention, f"{key}_days"),
                getattr(retention, f"{key}_years"),
            )
            for key in _RETENTION_KEYS
        }
        # attribut non resaisi : on garde la valeur déjà en base (en jours)
        for key in _RETENTION_KEYS:
            if totals[key] is None:
                totals[key] = bucket[f"retention_{key}"]

    default, minimum, maximum = totals["default"], totals["minimum"], totals["maximum"]

    if default is None or minimum is None or maximum is None:
        logging.info("validate_immutability9b")
        errors.append(
            "Retention configuration is not valid. "
            "You must set default, minimum and maximum (in days and/or years)."
        )
    else:
        if minimum <= 0 or minimum > default:
            logging.info("validate_immutability10")
            errors.append(
                f"Retention minimum ({minimum} days) cannot be inferior or equal to 0 "
                f"nor superior to default ({default} days)."
            )

        if default < minimum or default >= maximum:
            logging.info("validate_immutability11")
            errors.append(
                f"Retention default ({default} days) cannot be inferior to minimum ({minimum} days) "
                f"nor superior or equal to maximum ({maximum} days)."
            )

        if maximum > MAX_RETENTION_DAYS or maximum <= default:
            logging.info("validate_immutability12")
            errors.append(
                f"Retention maximum ({maximum} days) cannot be inferior or equal to default "
                f"({default} days) nor superior to 5 years ({MAX_RETENTION_DAYS} days)."
            )

        existing_immutability["retention"] = {
            "retention_enabled": True,
            "default": default,
            "minimum": minimum,
            "maximum": maximum,
        }
        logging.info(f"validate_immutability13 : {existing_immutability}")

    if errors:
        global_message = " | ".join(errors)
        raise DeclineDemandException(global_message)

    return existing_immutability
