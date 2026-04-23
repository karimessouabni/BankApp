from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


COS_CONFIG_ENDPOINT = "https://config.cloud-object-storage.cloud.ibm.com/v1"
ACTIVE_RESTORE_STATUSES = {"initializing", "running"}
COMPLETED_RESTORE_STATUSES = {"complete"}
FAILED_RESTORE_STATUSES = {"failed"}

BACKUP_VAULT_NAME_PATTERN = re.compile(
    r"^(?!\d{1,3}(?:\.\d{1,3}){3}$)[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$"
)


class CosConfigurationApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class BackupVaultValidationError(ValueError):
    pass


@dataclass(frozen=True)
class BackupVaultService:
    bearer_token: str
    endpoint: str = COS_CONFIG_ENDPOINT
    timeout: int = 60

    def get_backup_vault(self, backup_vault_name: str) -> dict[str, Any] | None:
        self._validate_backup_vault_name(backup_vault_name)

        try:
            return self._request(
                "GET",
                f"/backup_vaults/{self._quote(backup_vault_name)}",
            )
        except CosConfigurationApiError as exc:
            if exc.status_code == 404:
                return None
            raise

    def require_backup_vault(self, backup_vault_name: str) -> dict[str, Any]:
        backup_vault = self.get_backup_vault(backup_vault_name)
        if backup_vault is None:
            raise BackupVaultValidationError(
                f"Backup vault '{backup_vault_name}' does not exist."
            )
        return backup_vault

    def list_recovery_ranges(
        self,
        backup_vault_name: str,
        source_resource_crn: str | None = None,
    ) -> list[dict[str, Any]]:
        self._validate_backup_vault_name(backup_vault_name)

        params: dict[str, str] = {}
        if source_resource_crn:
            params["source_resource_crn"] = source_resource_crn

        return self._list_all(
            path=f"/backup_vaults/{self._quote(backup_vault_name)}/recovery_ranges",
            collection_key="recovery_ranges",
            params=params,
        )

    def require_recovery_ranges(
        self,
        backup_vault_name: str,
        source_resource_crn: str | None = None,
    ) -> list[dict[str, Any]]:
        recovery_ranges = self.list_recovery_ranges(
            backup_vault_name=backup_vault_name,
            source_resource_crn=source_resource_crn,
        )
        if not recovery_ranges:
            bucket_context = (
                f" and source bucket '{source_resource_crn}'"
                if source_resource_crn
                else ""
            )
            raise BackupVaultValidationError(
                f"No recovery range found for backup vault '{backup_vault_name}'"
                f"{bucket_context}."
            )
        return recovery_ranges

    def list_restores(self, backup_vault_name: str) -> list[dict[str, Any]]:
        self._validate_backup_vault_name(backup_vault_name)

        return self._list_all(
            path=f"/backup_vaults/{self._quote(backup_vault_name)}/restores",
            collection_key="restores",
        )

    def find_active_restore_for_target_bucket(
        self,
        backup_vault_name: str,
        target_resource_crn: str,
    ) -> dict[str, Any] | None:
        for restore in self.list_restores(backup_vault_name):
            status = self.normalize_restore_status(restore)
            restore_target = restore.get("target_resource_crn")

            if status in ACTIVE_RESTORE_STATUSES and restore_target == target_resource_crn:
                return restore

        return None

    def get_restore(
        self,
        backup_vault_name: str,
        restore_id: str,
    ) -> dict[str, Any]:
        self._validate_backup_vault_name(backup_vault_name)

        return self._request(
            "GET",
            f"/backup_vaults/{self._quote(backup_vault_name)}/restores/{self._quote(restore_id)}",
        )

    def create_restore(
        self,
        backup_vault_name: str,
        recovery_range_id: str,
        restore_point_in_time: str,
        target_resource_crn: str,
        restore_type: str = "in_place",
    ) -> dict[str, Any]:
        self._validate_backup_vault_name(backup_vault_name)

        payload = {
            "recovery_range_id": recovery_range_id,
            "restore_point_in_time": restore_point_in_time,
            "restore_type": restore_type,
            "target_resource_crn": target_resource_crn,
        }

        return self._request(
            "POST",
            f"/backup_vaults/{self._quote(backup_vault_name)}/restores",
            payload=payload,
        )

    @staticmethod
    def normalize_restore_status(restore: dict[str, Any]) -> str:
        return str(restore.get("restore_status", "")).strip().lower()

    @staticmethod
    def is_same_restore_request(
        restore: dict[str, Any],
        recovery_range_id: str,
        restore_point_in_time: str,
        target_resource_crn: str,
    ) -> bool:
        return (
            restore.get("recovery_range_id") == recovery_range_id
            and restore.get("restore_point_in_time") == restore_point_in_time
            and restore.get("target_resource_crn") == target_resource_crn
        )

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._build_url(path, params)
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(
            url=url,
            data=body,
            headers=self._headers(),
            method=method,
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response_body = response.read().decode("utf-8")
                return json.loads(response_body) if response_body else {}
        except urllib.error.HTTPError as exc:
            error_payload = self._decode_error_payload(exc)
            raise CosConfigurationApiError(
                message=f"IBM COS Configuration API returned HTTP {exc.code} for {method} {url}",
                status_code=exc.code,
                payload=error_payload,
            ) from exc
        except urllib.error.URLError as exc:
            raise CosConfigurationApiError(
                message=f"Unable to call IBM COS Configuration API for {method} {url}: {exc.reason}",
            ) from exc

    def _list_all(
        self,
        path: str,
        collection_key: str,
        params: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        current_params = dict(params or {})
        items: list[dict[str, Any]] = []

        while True:
            payload = self._request("GET", path, params=current_params)
            items.extend(payload.get(collection_key, []))

            next_token = self._extract_next_token(payload)
            if not next_token:
                return items

            current_params["token"] = next_token

    def _build_url(self, path: str, params: dict[str, str] | None = None) -> str:
        base_url = f"{self.endpoint.rstrip('/')}{path}"
        if not params:
            return base_url

        return f"{base_url}?{urllib.parse.urlencode(params)}"

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.bearer_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    @staticmethod
    def _quote(value: str) -> str:
        return urllib.parse.quote(value, safe="")

    @staticmethod
    def _extract_next_token(payload: dict[str, Any]) -> str | None:
        next_value = payload.get("next")
        if isinstance(next_value, dict):
            token = next_value.get("token")
            return str(token) if token else None

        if isinstance(next_value, str) and next_value:
            parsed = urllib.parse.urlparse(next_value)
            tokens = urllib.parse.parse_qs(parsed.query).get("token")
            return tokens[0] if tokens else None

        return None

    @staticmethod
    def _decode_error_payload(exc: urllib.error.HTTPError) -> Any:
        raw_body = exc.read().decode("utf-8")
        if not raw_body:
            return None

        try:
            return json.loads(raw_body)
        except json.JSONDecodeError:
            return raw_body

    @staticmethod
    def _validate_backup_vault_name(backup_vault_name: str) -> None:
        if not BACKUP_VAULT_NAME_PATTERN.fullmatch(backup_vault_name):
            raise BackupVaultValidationError(
                f"Invalid backup_vault_name '{backup_vault_name}'. "
                "Must be 3-63 chars, lowercase alphanumeric with '.' or '-', "
                "and cannot be IP-formatted."
            )
