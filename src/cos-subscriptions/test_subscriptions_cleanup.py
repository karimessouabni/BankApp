"""Tests unitaires du filtrage (python -m pytest ou python test_subscriptions_cleanup.py)."""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import subscriptions_cleanup as sc  # noqa: E402


def _demand(action: str, status: str = "SUCCESS", uuid: str = "", create_date: str = "") -> dict:
    return {"action": action, "status": status, "status_reason": status.lower(),
            "uuid": uuid or f"uuid-{action}-{status}", "create_date": create_date}


def _row(subscription_id: str, demands: list[dict], name: str = "bu003i023571",
         user: str = "h90871") -> dict:
    return {
        "context": {"code_bu": "BP2I", "realm": "rl003i001058", "user": user,
                    "tier": "A", "env_type": "NPR"},
        "geninfo": {
            "apcode": "A100473",
            "demands": demands,
            "environment": "int",
            "region": "eu-de",
            "name": name,
            "product": "cos.bucket",
            "status": "ACTIVE",
            "subscription_id": subscription_id,
        },
        "specinfo": {"cos_instance": "co003i012219"},
    }


class IsEligibleTests(unittest.TestCase):
    def test_only_allowed_actions_in_success(self):
        self.assertTrue(sc.is_eligible([_demand("force_clean"), _demand("create"), _demand("update")]))

    def test_single_create(self):
        self.assertTrue(sc.is_eligible([_demand("create")]))

    def test_successful_delete_is_rejected(self):
        # Cas de la capture d'écran: force_clean + create + delete SUCCESS -> non éligible
        self.assertFalse(sc.is_eligible([_demand("force_clean"), _demand("create"), _demand("delete")]))

    def test_failed_delete_is_accepted(self):
        self.assertTrue(sc.is_eligible([_demand("force_clean"), _demand("create"), _demand("delete", "ERROR")]))
        self.assertTrue(sc.is_eligible([_demand("create"), _demand("delete", "FAILED"), _demand("delete", "ERROR")]))

    def test_failed_delete_with_failed_other_action_is_rejected(self):
        self.assertFalse(sc.is_eligible([_demand("create", "ERROR"), _demand("delete", "ERROR")]))

    def test_mixed_delete_statuses_is_rejected(self):
        self.assertFalse(sc.is_eligible([_demand("create"), _demand("delete", "ERROR"), _demand("delete")]))

    def test_unknown_action_is_rejected(self):
        self.assertFalse(sc.is_eligible([_demand("create"), _demand("restore")]))

    def test_failed_status_is_rejected(self):
        self.assertFalse(sc.is_eligible([_demand("create"), _demand("update", "FAILED")]))

    def test_empty_or_missing_is_rejected(self):
        self.assertFalse(sc.is_eligible([]))
        self.assertFalse(sc.is_eligible(None))


class FindEligibleTests(unittest.TestCase):
    def test_returns_only_eligible_ids(self):
        body = {
            "$schema": "https://orchestrator-gw.int.staging.echonet/schemas/SubscriptionsOutputBody.json",
            "result": {
                "product": "cos.bucket",
                "rows": [
                    _row("0d8022cd-5e47-48be-b4ac-b50d1bb54211",
                         [_demand("force_clean"), _demand("create"), _demand("delete")]),
                    _row("aaaa-1", [_demand("create"), _demand("update")], name="bu003i000001"),
                    _row("bbbb-2", [_demand("force_clean"), _demand("create")], name="bu003i000002"),
                    _row("cccc-3", [_demand("create", "FAILED")], name="bu003i000003"),
                    _row("dddd-4", [_demand("create"), _demand("delete", "ERROR")], name="bu003i000004"),
                ],
            },
        }
        found = sc.find_eligible_subscriptions(body)
        self.assertEqual([s.subscription_id for s in found], ["aaaa-1", "bbbb-2", "dddd-4"])
        self.assertEqual(found[0].actions, ["create", "update"])
        self.assertEqual(found[2].actions, ["create", "delete(ERROR)"])
        self.assertFalse(found[0].needs_retry)
        self.assertTrue(found[2].needs_retry)
        self.assertEqual(found[2].failed_delete_demand_ids, ["uuid-delete-ERROR"])
        self.assertEqual(found[0].name, "bu003i000001")

    def test_filters_on_context_user(self):
        body = {"result": {"rows": [
            _row("mine-1", [_demand("create")], user="h90871"),
            _row("other-1", [_demand("create")], user="service-account-products_cft_confidential"),
            _row("nouser-1", [_demand("create")]),
        ]}}
        body["result"]["rows"][2]["context"].pop("user")
        self.assertEqual([s.subscription_id for s in sc.find_eligible_subscriptions(body)], ["mine-1"])
        self.assertEqual(sc.find_eligible_subscriptions(body)[0].user, "h90871")
        self.assertEqual(
            [s.subscription_id for s in sc.find_eligible_subscriptions(body, "service-account-products_cft_confidential")],
            ["other-1"],
        )
        self.assertEqual(
            [s.subscription_id for s in sc.find_eligible_subscriptions(body, None)],
            ["mine-1", "other-1", "nouser-1"],
        )

    def test_cli_user_flags(self):
        self.assertEqual(sc.parse_args([]).user, "h90871")
        self.assertEqual(sc.parse_args(["--user", "h12345"]).user, "h12345")
        self.assertTrue(sc.parse_args(["--all-users"]).all_users)

    def test_row_without_subscription_id_is_skipped(self):
        row = _row("", [_demand("create")])
        self.assertEqual(sc.find_eligible_subscriptions({"result": {"rows": [row]}}), [])


class RetryHelpersTests(unittest.TestCase):
    def test_failed_delete_demand_ids_sorted_by_create_date(self):
        demands = [
            _demand("create"),
            _demand("delete", "ERROR", uuid="d2", create_date="2026-09-09T19:00:00Z"),
            _demand("delete", "ERROR", uuid="d1", create_date="2026-09-09T18:00:00Z"),
            _demand("delete", "SUCCESS", uuid="d3"),
        ]
        self.assertEqual(sc.failed_delete_demand_ids(demands), ["d1", "d2"])

    def test_failed_process_names(self):
        demand = {
            "kind": "Demand",
            "uuid": "182e47b2-cfa2-4829-bb01-c2110a96099b",
            "status": "IN_PROGRESS",
            "processes": [
                {"kind": "Process", "name": "bootstrap", "status": "SUCCESS"},
                {"kind": "Process", "name": "validate_bucket_and_workspace", "status": "ERROR"},
                {"kind": "Process", "name": "delete_bucket", "status": "ERROR"},
                {"kind": "Process", "name": "notify", "status": "PENDING"},
            ],
        }
        self.assertEqual(sc.failed_process_names(demand),
                         ["validate_bucket_and_workspace", "delete_bucket"])
        self.assertEqual(sc.failed_process_names({}), [])


class ClientTests(unittest.TestCase):
    def test_get_demand_url(self):
        client = sc.OrchestratorClient("tok")
        with mock.patch.object(client, "_request", return_value={}) as req:
            client.get_demand("182e47b2-cfa2-4829-bb01-c2110a96099b")
        req.assert_called_once_with("GET", "/api/v1/demands/182e47b2-cfa2-4829-bb01-c2110a96099b")

    def test_retry_demand_payload(self):
        client = sc.OrchestratorClient("tok")
        with mock.patch.object(client, "_request", return_value={}) as req:
            client.retry_demand("182e47b2", ["delete_bucket"])
        req.assert_called_once_with(
            "POST", "/api/v1/demands/182e47b2/retry",
            {"tasks": ["delete_bucket"], "retry_non_failed_tasks": False},
        )

    def test_retry_failed_delete_end_to_end(self):
        client = sc.OrchestratorClient("tok")
        demand = {"processes": [{"name": "bootstrap", "status": "SUCCESS"},
                                {"name": "delete_bucket", "status": "ERROR"}]}
        with mock.patch.object(client, "get_demand", return_value=demand), \
             mock.patch.object(client, "retry_demand", return_value={}) as retry:
            self.assertEqual(client.retry_failed_delete("d1"), ["delete_bucket"])
        retry.assert_called_once_with("d1", ["delete_bucket"])

    def test_retry_failed_delete_skips_when_nothing_in_error(self):
        client = sc.OrchestratorClient("tok")
        demand = {"processes": [{"name": "bootstrap", "status": "SUCCESS"}]}
        with mock.patch.object(client, "get_demand", return_value=demand), \
             mock.patch.object(client, "retry_demand") as retry:
            self.assertEqual(client.retry_failed_delete("d1"), [])
        retry.assert_not_called()

    def test_delete_payload_and_url(self):
        client = sc.OrchestratorClient("tok", "https://orchestrator-gw.int.staging.echonet/")
        with mock.patch.object(client, "_request", return_value={"ok": True}) as req:
            client.delete_subscription("0d8022cd-5e47-48be-b4ac-b50d1bb54211")
        req.assert_called_once_with(
            "DELETE",
            "/apl/v1/subscriptions/0d8022cd-5e47-48be-b4ac-b50d1bb54211",
            {"product_branch": "main", "payload": {}},
        )

    def test_get_url(self):
        client = sc.OrchestratorClient("tok")
        with mock.patch.object(client, "_request", return_value={"result": {"rows": []}}) as req:
            client.get_subscriptions("cos.bucket")
        req.assert_called_once_with("GET", "/multireader/api/v1/subscriptions?product=cos.bucket")

    def test_request_sets_bearer_header(self):
        client = sc.OrchestratorClient("tok")
        captured = {}

        class _Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"ok": True}).encode()

        def fake_urlopen(request, timeout=None, context=None):
            captured["request"] = request
            return _Resp()

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            out = client._request("DELETE", "/x", {"product_branch": "main", "payload": {}})
        req = captured["request"]
        self.assertEqual(out, {"ok": True})
        self.assertEqual(req.get_method(), "DELETE")
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        self.assertEqual(json.loads(req.data), {"product_branch": "main", "payload": {}})

    def test_missing_token_rejected(self):
        with self.assertRaises(ValueError):
            sc.OrchestratorClient("")


if __name__ == "__main__":
    unittest.main()
