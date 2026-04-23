from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any

from airflow import DAG
from airflow.decorators import task
from airflow.exceptions import AirflowException
from airflow.sensors.python import PythonSensor

from backup_vault_service import (
    ACTIVE_RESTORE_STATUSES,
    COMPLETED_RESTORE_STATUSES,
    FAILED_RESTORE_STATUSES,
    BackupVaultService,
    BackupVaultValidationError,
)


IBM_IAM_TOKEN_URL = "https://iam.cloud.ibm.com/identity/token"
IBM_API_KEY_ENV = "IBM_CLOUD_API_KEY"
RESTORE_STATUS_TASK_ID = "trigger_restore"


def get_iam_access_token(api_key: str) -> str:
    encoded_body = urllib.parse.urlencode(
        {
            "grant_type": "urn:ibm:params:oauth:grant-type:apikey",
            "apikey": api_key,
        }
    ).encode("utf-8")

    request = urllib.request.Request(
        url=IBM_IAM_TOKEN_URL,
        data=encoded_body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode("utf-8"))
        return payload["access_token"]


def build_backup_vault_service() -> BackupVaultService:
    api_key = os.environ.get(IBM_API_KEY_ENV)
    if not api_key:
        raise AirflowException(f"Missing required environment variable {IBM_API_KEY_ENV}.")

    return BackupVaultService(bearer_token=get_iam_access_token(api_key))


def read_restore_conf(dag_run_conf: dict[str, Any]) -> dict[str, str]:
    required_keys = {
        "backup_vault_name",
        "recovery_range_id",
        "restore_point_in_time",
        "source_resource_crn",
        "target_resource_crn",
    }
    missing_keys = sorted(key for key in required_keys if not dag_run_conf.get(key))

    if missing_keys:
        raise AirflowException(
            "Missing required restore configuration keys: "
            + ", ".join(missing_keys)
        )

    return {key: str(dag_run_conf[key]) for key in required_keys}


with DAG(
    dag_id="ibm_cos_bucket_restore",
    description="Restore IBM COS objects from a backup vault to a target bucket.",
    start_date=datetime(2024, 1, 1),
    schedule=None,
    catchup=False,
    max_active_runs=1,
    default_args={
        "retries": 1,
        "retry_delay": timedelta(minutes=5),
    },
    tags=["ibm", "cos", "backup-vault", "restore"],
) as dag:

    @task
    def validate_restore_request(**context: Any) -> dict[str, Any]:
        restore_conf = read_restore_conf(context["dag_run"].conf or {})
        service = build_backup_vault_service()

        try:
            backup_vault = service.require_backup_vault(
                restore_conf["backup_vault_name"]
            )
            recovery_ranges = service.require_recovery_ranges(
                backup_vault_name=restore_conf["backup_vault_name"],
                source_resource_crn=restore_conf["source_resource_crn"],
            )
        except BackupVaultValidationError as exc:
            raise AirflowException(str(exc)) from exc

        matching_range = next(
            (
                recovery_range
                for recovery_range in recovery_ranges
                if recovery_range.get("recovery_range_id")
                == restore_conf["recovery_range_id"]
            ),
            None,
        )

        if matching_range is None:
            raise AirflowException(
                "Recovery range "
                f"'{restore_conf['recovery_range_id']}' does not exist for "
                f"backup vault '{restore_conf['backup_vault_name']}' and "
                f"source bucket '{restore_conf['source_resource_crn']}'."
            )

        active_restore = service.find_active_restore_for_target_bucket(
            backup_vault_name=restore_conf["backup_vault_name"],
            target_resource_crn=restore_conf["target_resource_crn"],
        )

        if active_restore:
            if service.is_same_restore_request(
                active_restore,
                recovery_range_id=restore_conf["recovery_range_id"],
                restore_point_in_time=restore_conf["restore_point_in_time"],
                target_resource_crn=restore_conf["target_resource_crn"],
            ):
                return {
                    **restore_conf,
                    "backup_vault": backup_vault,
                    "selected_recovery_range": matching_range,
                    "existing_restore_id": active_restore["restore_id"],
                }

            raise AirflowException(
                "A restore is already active for target bucket "
                f"'{restore_conf['target_resource_crn']}' in backup vault "
                f"'{restore_conf['backup_vault_name']}'. "
                f"Existing restore_id={active_restore.get('restore_id')}, "
                f"status={active_restore.get('restore_status')}."
            )

        return {
            **restore_conf,
            "backup_vault": backup_vault,
            "selected_recovery_range": matching_range,
        }

    @task(task_id=RESTORE_STATUS_TASK_ID)
    def trigger_restore(validated_request: dict[str, Any]) -> dict[str, Any]:
        existing_restore_id = validated_request.get("existing_restore_id")
        if existing_restore_id:
            return {
                "backup_vault_name": validated_request["backup_vault_name"],
                "restore_id": existing_restore_id,
                "reused_existing_restore": True,
            }

        service = build_backup_vault_service()
        restore = service.create_restore(
            backup_vault_name=validated_request["backup_vault_name"],
            recovery_range_id=validated_request["recovery_range_id"],
            restore_point_in_time=validated_request["restore_point_in_time"],
            target_resource_crn=validated_request["target_resource_crn"],
        )

        return {
            "backup_vault_name": validated_request["backup_vault_name"],
            "restore_id": restore["restore_id"],
            "restore_status": restore.get("restore_status"),
            "reused_existing_restore": False,
        }

    def poll_restore_status(**context: Any) -> bool:
        restore_context = context["ti"].xcom_pull(task_ids=RESTORE_STATUS_TASK_ID)
        if not restore_context:
            raise AirflowException("Missing restore context in XCom.")

        service = build_backup_vault_service()
        restore = service.get_restore(
            backup_vault_name=restore_context["backup_vault_name"],
            restore_id=restore_context["restore_id"],
        )

        status = service.normalize_restore_status(restore)
        progress = restore.get("restore_percent_progress")
        print(
            "Restore status: "
            f"restore_id={restore_context['restore_id']} "
            f"status={status} progress={progress}"
        )

        if status in COMPLETED_RESTORE_STATUSES:
            return True

        if status in FAILED_RESTORE_STATUSES:
            raise AirflowException(
                "Restore failed: "
                f"restore_id={restore_context['restore_id']} "
                f"error={restore.get('error_cause', 'unknown')}"
            )

        if status in ACTIVE_RESTORE_STATUSES:
            return False

        raise AirflowException(
            "Unexpected restore status: "
            f"restore_id={restore_context['restore_id']} status={status}"
        )

    validated_request = validate_restore_request()
    restore_context = trigger_restore(validated_request)

    wait_restore_completion = PythonSensor(
        task_id="wait_restore_completion",
        python_callable=poll_restore_status,
        poke_interval=300,
        timeout=86400,
        mode="reschedule",
    )

    validated_request >> restore_context >> wait_restore_completion
