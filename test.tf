@step
def get_latest_recovery_range(
    iam_token,
    input_user_validation: dict,
    payload: BucketRestoreBackupVaultPayload = depends(payload_dependency),
) -> dict:
    from cos_service.services.restore_service import list_recovery_ranges

    source_bucket = input_user_validation["source_bucket"]
    ranges = list_recovery_ranges(
        payload.backup_vault_name, source_bucket["bucket_crn"], iam_token
    )
    logger.info("recovery_ranges count=%d for bucket=%s", len(ranges), source_bucket["name"])

    if not ranges:
        raise ValueError(
            f"No recovery range found for bucket {source_bucket['name']} "
            f"in vault {payload.backup_vault_name}"
        )

    # Range explicitement demandé → il doit exister, sinon on refuse
    if payload.recovery_range_id is not None:
        wanted = next(
            (r for r in ranges if r["recovery_range_id"] == payload.recovery_range_id),
            None,
        )
        if wanted is None:
            raise ValueError(
                f"recovery_range_id {payload.recovery_range_id} not found "
                f"(available: {[r['recovery_range_id'] for r in ranges]})"
            )
        return wanted

    # Sinon : le plus récent, explicitement trié
    return max(ranges, key=lambda r: r["range_end_time"])
