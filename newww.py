import logging

# Adapter le chemin d'import au vrai projet (ex. cos_service.models.bucket_retention).
# DeclineDemandException vient du framework applicatif.
from bucket_retention import (
    DAYS,
    MAX_RETENTION_YEARS,
    YEARS,
    BucketRetention,
    max_retention,
    max_retention_days,
    years_to_days,
)

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

    Les règles de payload (unité unique, valeurs > 0, min ≤ default ≤ max,
    plafond 5 ans bissextiles comprises) sont déjà garanties par les validateurs
    du modèle : un BucketRetention invalide ne peut pas exister. Ici on n'exige
    que la complétude — les trois attributs — puis on normalise en jours.
    """
    unit = retention.unit
    default, minimum, maximum = retention.default, retention.minimum, retention.maximum

    if None in (unit, default, minimum, maximum):
        logging.info("compute_bucket_retention1")
        raise DeclineDemandException(
            "Retention configuration is not valid. You must set default, minimum and maximum "
            "in the same unit, either in days (…_days) or in years (…_years)."
        )

    immutability["retention"]["retention_enabled"] = retention.retention_enabled
    for key in _RETENTION_KEYS:
        immutability["retention"][key] = retention.in_days(key)

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
    """Rétention côté update : tout est normalisé, comparé et stocké en jours.

    Un attribut non resaisi est repris des colonnes retention_* du bucket
    (déjà en jours) ; une saisie en années est convertie en jours (calendaire),
    ce qui permet aussi de changer d'unité d'un update à l'autre.
    """
    existing_retention = existing_immutability["retention"]

    if not retention or retention.is_empty() or not retention.retention_enabled:
        logging.info("validate_immutability6")
        values = {key: existing_retention[key] for key in _RETENTION_KEYS}
    else:
        values = {key: retention.in_days(key) for key in _RETENTION_KEYS}
        for key in _RETENTION_KEYS:
            if values[key] is None:
                values[key] = bucket[f"retention_{key}"]

    default, minimum, maximum = values["default"], values["minimum"], values["maximum"]

    if default is None or minimum is None or maximum is None:
        logging.info("validate_immutability7")
        errors.append(
            "Retention configuration is not valid. You must set default, minimum and maximum "
            "in the same unit, either in days or in years."
        )
        return

    check_retention_bounds(DAYS, default, minimum, maximum, errors)

    existing_immutability["retention"] = {
        "retention_enabled": True,
        "default": default,
        "minimum": minimum,
        "maximum": maximum,
    }
    logging.info(f"validate_immutability8 : {existing_immutability}")


def _backup_requested(backup) -> bool:
    return bool(backup and not backup.is_empty() and backup.backup_enabled)


def _apply_versioning_only_update(existing_immutability: dict, enable_versioning) -> None:
    """Choix NONE : seule la bascule de versioning est appliquée, le reste est conservé."""
    if enable_versioning is None:
        enable_versioning = existing_immutability["object_versioning_enabled"]
    existing_immutability["object_versioning_enabled"] = enable_versioning
    logging.info(f"apply_versioning_only : {existing_immutability}")


def _retention_requested(payload_retention) -> bool:
    return bool(payload_retention and not payload_retention.is_empty()
                and payload_retention.retention_enabled)


def _object_lock_requested(object_lock_duration_days, object_lock_duration_years) -> bool:
    return object_lock_duration_days is not None or object_lock_duration_years is not None


def _infer_immutability_choice(payload_retention, object_lock_duration_days, object_lock_duration_years):
    """Choix implicite quand le payload n'en donne pas : déduit de ce qui est saisi."""
    retention_given = _retention_requested(payload_retention)
    lock_given = _object_lock_requested(object_lock_duration_days, object_lock_duration_years)

    if retention_given and lock_given:
        raise DeclineDemandException(
            "Retention and object Lock are not compatible. "
            "We cannot activate both of them simultaneously."
        )
    if retention_given:
        return Immutability.RETENTION
    if lock_given:
        return Immutability.OBJECT_LOCK
    return Immutability.NONE


def _new_retention_immutability(payload_retention, enable_versioning, backup, immutability: dict) -> dict:
    """Nouvelle immutabilité en mode rétention : incompatibilités puis calcul."""
    errors = []

    if enable_versioning:
        errors.append(
            "Retention and versioning are not compatible. "
            "We cannot activate both of them simultaneously."
        )
    if _backup_requested(backup):
        errors.append(
            "Retention and bucket backup are not compatible. "
            "We cannot activate backup when retention is enabled."
        )
    if not _retention_requested(payload_retention):
        errors.append("Retention must not be empty.")

    raise_on_errors(errors)
    return compute_bucket_retention(payload_retention, immutability)


def _new_object_lock_immutability(
    object_lock_duration_days,
    object_lock_duration_years,
    enable_versioning,
    immutability: dict,
) -> dict:
    """Nouvelle immutabilité en mode object-lock : versioning requis, durée obligatoire."""
    errors = []

    if not enable_versioning:
        errors.append("Versioning should be enabled to enable object-lock.")
    if not _object_lock_requested(object_lock_duration_days, object_lock_duration_years):
        errors.append("Object Lock Duration Days or Years must be not empty.")

    raise_on_errors(errors)
    return compute_bucket_object_lock(
        immutability, object_lock_duration_days, object_lock_duration_years
    )


def compute_bucket_new_immutability(
    immutability_choice,
    payload_retention,
    object_lock_duration_days,
    object_lock_duration_years,
    enable_versioning,
    immutability: dict,
    backup,
) -> dict:
    """Construit le bloc immutability d'un bucket qui n'en avait pas encore.

    Sans choix explicite dans le payload, le choix est déduit de ce qui est
    saisi : rétention -> RETENTION, durée d'object-lock -> OBJECT_LOCK, sinon
    NONE (seule la bascule de versioning est appliquée).
    """
    if immutability_choice is None:
        immutability_choice = _infer_immutability_choice(
            payload_retention, object_lock_duration_days, object_lock_duration_years
        )
        logging.info(f"compute_bucket_new_immutability choix déduit : {immutability_choice}")

    if immutability_choice == Immutability.RETENTION:
        immutability = _new_retention_immutability(
            payload_retention, enable_versioning, backup, immutability
        )
    elif immutability_choice == Immutability.OBJECT_LOCK:
        immutability = _new_object_lock_immutability(
            object_lock_duration_days,
            object_lock_duration_years,
            enable_versioning,
            immutability,
        )
    else:
        _apply_versioning_only_update(immutability, enable_versioning)

    if _backup_requested(backup):
        immutability = compute_bucket_backup(immutability, backup)

    logging.info(f"compute_bucket_new_immutability : {immutability}")
    return immutability


def _update_object_locked_bucket(
    existing_immutability: dict,
    retention,
    enable_versioning,
    object_lock_duration_days,
    object_lock_duration_years,
    errors,
) -> None:
    """Bucket déjà en object-lock : ni rétention ni arrêt du versioning, durée remplaçable."""
    if retention is not None:
        errors.append("Setting a retention is not possible when object-lock is already enabled.")

    if enable_versioning is False:
        errors.append("Disabling versioning is not possible when object-lock is already enabled.")

    validate_object_lock_update(
        existing_immutability,
        object_lock_duration_days,
        object_lock_duration_years,
        errors,
    )


def _update_retention_bucket(
    existing_immutability: dict,
    bucket: dict,
    retention,
    backup,
    enable_versioning,
    object_lock_duration_days,
    object_lock_duration_years,
    errors,
) -> None:
    """Bucket déjà en rétention : ni object-lock, ni versioning, ni backup."""
    if object_lock_duration_days is not None or object_lock_duration_years is not None:
        errors.append("Setting an object-lock is not possible when retention is already enabled.")

    if enable_versioning:
        errors.append("Enabling versioning is not possible when retention is already enabled.")

    if _backup_requested(backup):
        errors.append(
            "Retention and bucket backup are not compatible. "
            "We cannot activate backup when retention is enabled."
        )

    validate_retention_update(retention, bucket, existing_immutability, errors)


def validate_immutability_for_update_bucket(
    bucket: dict,
    immutability_choice,
    retention,
    object_lock_duration_days: int | None,
    object_lock_duration_years: int | None,
    enable_versioning,
    has_contents: bool,
    backup,
    session,
) -> dict:
    """Orchestration de l'update : valide la demande contre l'état actuel du bucket.

    Immutability, compute_bucket_immutability_choice, compute_bucket_new_immutability
    et compute_bucket_backup viennent du reste du service. Toutes les erreurs sont
    accumulées puis levées en une seule DeclineDemandException.
    """
    existing_immutability = compute_bucket_immutability_for_update_bucket(bucket, session)
    existing_choice = compute_bucket_immutability_choice(bucket)
    errors = []

    if immutability_choice == Immutability.NONE:
        _apply_versioning_only_update(existing_immutability, enable_versioning)

    if existing_choice is None:
        logging.info("validate_immutability2")
        existing_immutability = compute_bucket_new_immutability(
            immutability_choice,
            retention,
            object_lock_duration_days,
            object_lock_duration_years,
            enable_versioning,
            existing_immutability,
            backup,
        )

    if existing_immutability["retention"]["retention_enabled"] and has_contents:
        errors.append("Setting a retention is not possible when the bucket already contains objects.")

    if existing_choice == Immutability.OBJECT_LOCK:
        logging.info("validate_immutability3")
        _update_object_locked_bucket(
            existing_immutability,
            retention,
            enable_versioning,
            object_lock_duration_days,
            object_lock_duration_years,
            errors,
        )

    if existing_choice == Immutability.RETENTION:
        logging.info("validate_immutability8")
        _update_retention_bucket(
            existing_immutability,
            bucket,
            retention,
            backup,
            enable_versioning,
            object_lock_duration_days,
            object_lock_duration_years,
            errors,
        )

    raise_on_errors(errors)

    if _backup_requested(backup) and (
        immutability_choice == Immutability.NONE or existing_choice == Immutability.OBJECT_LOCK
    ):
        logging.info(f"validate_immutability14 : {backup}")
        existing_immutability = compute_bucket_backup(existing_immutability, backup)

    logging.info(f"validate_immutability15 : {existing_immutability}")
    return existing_immutability


def _retention_from_bucket(bucket: dict) -> dict:
    """Bloc retention (en jours) depuis la ligne bucket ; valeurs à None si désactivée."""
    enabled = bool(bucket["retention_enabled"])
    return {
        "retention_enabled": enabled,
        **{key: bucket[f"retention_{key}"] if enabled else None for key in _RETENTION_KEYS},
    }


def _backup_from_bucket(bucket: dict, session) -> dict:
    """Bloc backup depuis la ligne bucket ; le vault n'est résolu que si nécessaire."""
    if not bucket["backup_enabled"]:
        return {"backup_enabled": False, "backup_vault_sub_id": None, "backup_retention_days": None}

    from cos_service.services.backup_vault_service import get_backup_vault_by_sub_id

    backup_vault = get_backup_vault_by_sub_id(bucket["backup_vault_subscription_id"], session)
    return {
        "backup_enabled": True,
        "backup_vault_sub_id": backup_vault.subscription_id,
        "backup_retention_days": bucket["backup_retention_days"],
    }


def compute_bucket_immutability_for_update_bucket(bucket: dict, session) -> dict:
    """Reconstruit le bloc immutability existant depuis la ligne bucket en base.

    Garantit qu'une seule unité d'object lock en sort : c'est ce dict qui part
    ensuite vers Terraform, où les deux variables à la fois créent un conflit.
    """
    object_lock_duration_days = bucket["object_lock_duration_days"]
    object_lock_duration_years = bucket.get("object_lock_duration_years")

    if object_lock_duration_days is not None and object_lock_duration_years is not None:
        # Ligne héritée d'un ancien update qui n'effaçait pas l'autre unité :
        # on garde les jours (colonne d'origine) plutôt que de bloquer le client.
        logging.warning(
            "bucket stores object lock in both units "
            f"(days={object_lock_duration_days}, years={object_lock_duration_years}), keeping days"
        )
        object_lock_duration_years = None

    immutability = {
        "object_locking_enabled": object_lock_duration_days is not None
        or object_lock_duration_years is not None,
        "object_versioning_enabled": bool(bucket.get("object_versioning_enabled")),
        "object_lock_duration_days": object_lock_duration_days,
        "object_lock_duration_years": object_lock_duration_years,
        "retention": _retention_from_bucket(bucket),
        "backup": _backup_from_bucket(bucket, session),
    }
    logging.info(f"compute_bucket_immutability_for_update_bucket : {immutability}")
    return immutability
