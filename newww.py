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
