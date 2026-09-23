"""Tests unitaires du filtrage (python -m pytest ou python test_subscriptions_cleanup.py)."""

from __future__ import annotations

import http.server
import json
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
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


class PaginationTests(unittest.TestCase):
    def _fetch(self, pages: dict, calls: list):
        def fetch(page, size):
            calls.append((page, size))
            return pages.get(page, {"result": {"rows": []}})
        return fetch

    def test_stops_on_short_page(self):
        calls = []
        pages = {1: {"result": {"rows": [_row("a", []), _row("b", [])]}},
                 2: {"result": {"rows": [_row("c", [])]}}}
        rows = sc.iterate_pages(self._fetch(pages, calls), size=2)
        self.assertEqual([sc.subscription_uuid(r) for r in rows], ["a", "b", "c"])
        self.assertEqual(calls, [(1, 2), (2, 2)])

    def test_stops_on_empty_page(self):
        calls = []
        pages = {1: {"result": {"rows": [_row("a", []), _row("b", [])]}}}
        rows = sc.iterate_pages(self._fetch(pages, calls), size=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(calls, [(1, 2), (2, 2)])

    def test_stops_on_total_pages_hint(self):
        calls = []
        pages = {1: {"result": {"rows": [_row("a", []), _row("b", [])], "total_pages": 2}},
                 2: {"result": {"rows": [_row("c", []), _row("d", [])], "total_pages": 2}},
                 3: {"result": {"rows": [_row("zz", []), _row("zy", [])], "total_pages": 2}}}
        rows = sc.iterate_pages(self._fetch(pages, calls), size=2)
        self.assertEqual([sc.subscription_uuid(r) for r in rows], ["a", "b", "c", "d"])
        self.assertEqual(calls, [(1, 2), (2, 2)])

    def test_total_count_hint(self):
        self.assertEqual(sc.total_pages_hint({"total": 250}, 100), 3)
        self.assertEqual(sc.total_pages_hint({"result": {"totalElements": 200}}, 100), 2)
        self.assertEqual(sc.total_pages_hint({"pagination": {"totalPages": 4}}, 100), 4)
        self.assertEqual(sc.total_pages_hint({"total": 0}, 100), 0)
        self.assertIsNone(sc.total_pages_hint({"result": {"rows": []}}, 100))
        self.assertIsNone(sc.total_pages_hint([], 100))

    def test_stops_when_api_ignores_page_param(self):
        calls = []
        same = {"result": {"rows": [_row("a", []), _row("b", [])]}}
        rows = sc.iterate_pages(lambda p, s: (calls.append(p), same)[1], size=2)
        self.assertEqual(len(rows), 2)
        self.assertEqual(calls, [1, 2])

    def test_first_page_and_max_pages(self):
        calls = []
        full = lambda p, s: (calls.append(p), {"result": {"rows": [_row(f"a{p}", []), _row(f"b{p}", [])]}})[1]
        rows = sc.iterate_pages(full, size=2, first_page=0, max_pages=3)
        self.assertEqual(calls, [0, 1, 2])
        self.assertEqual(len(rows), 6)

    def test_accepts_bare_list_pages(self):
        pages = {1: [_row("a", [])]}
        rows = sc.iterate_pages(lambda p, s: pages.get(p, []), size=2)
        self.assertEqual(len(rows), 1)

    def test_dedupe_rows(self):
        rows = sc.dedupe_rows([_row("a", []), _row("b", []), _row("a", []), {"x": 1}, {"y": 2}])
        self.assertEqual(len(rows), 4)

    def test_cli_pagination_flags(self):
        args = sc.parse_args(["--page-size", "50", "--first-page", "0"])
        self.assertEqual((args.page_size, args.first_page), (50, 0))
        self.assertEqual((sc.parse_args([]).page_size, sc.parse_args([]).first_page), (100, 1))
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            sc.parse_args(["--page-size", "0"])


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
        req.assert_called_once_with("GET", "/multireader/api/v1/subscriptions?page=1&size=100")
        with mock.patch.object(client, "_request", return_value={"result": {"rows": []}}) as req:
            client.get_subscriptions("cos.bucket", page_size=50, first_page=0)
        req.assert_called_once_with("GET", "/multireader/api/v1/subscriptions?page=0&size=50")

    def test_get_subscriptions_walks_all_pages(self):
        client = sc.OrchestratorClient("tok")
        pages = {
            "/multireader/api/v1/subscriptions?page=1&size=2": {"result": {"rows": [_row("s1", []), _row("s2", [])]}},
            "/multireader/api/v1/subscriptions?page=2&size=2": {"result": {"rows": [_row("s3", []), _row("s4", [])]}},
            "/multireader/api/v1/subscriptions?page=3&size=2": {"result": {"rows": [_row("s5", [])]}},
        }
        seen = []
        with mock.patch.object(client, "_request", side_effect=lambda m, path: pages[path]) as req:
            body = client.get_subscriptions("cos.bucket", page_size=2, progress=lambda p, n: seen.append((p, n)))
        self.assertEqual([r["geninfo"]["subscription_id"] for r in body["result"]["rows"]],
                         ["s1", "s2", "s3", "s4", "s5"])
        self.assertEqual(req.call_count, 3)
        self.assertEqual(seen, [(1, 2), (2, 2), (3, 1)])

    def test_get_subscriptions_filters_product(self):
        client = sc.OrchestratorClient("tok")
        other = _row("s2", [])
        other["geninfo"]["product"] = "cos.instance"
        no_product = _row("s3", [])
        del no_product["geninfo"]["product"]
        body = {"result": {"rows": [_row("s1", []), other, no_product]}}
        with mock.patch.object(client, "_request", return_value=body):
            rows = client.get_subscriptions("cos.bucket")["result"]["rows"]
            self.assertEqual([r["geninfo"]["subscription_id"] for r in rows], ["s1", "s3"])
            rows = client.get_subscriptions(None)["result"]["rows"]
            self.assertEqual(len(rows), 3)

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


def _err_row(uuid: str, statuses: list[str] | None = None, user: str = "h90871",
             sub_status: str = "ACTIVE", name: str = "bu003i023571") -> dict:
    """Row du listing multireader dont les demandes ont les statuts donnés (ON_ERROR par défaut)."""
    statuses = ["ON_ERROR"] if statuses is None else statuses
    demands = [_demand(["create", "update", "delete"][i % 3], st, uuid=f"{uuid}-d{i}", create_date=f"2026-0{i + 1}")
               for i, st in enumerate(statuses)]
    row = _row(uuid, demands, name=name, user=user)
    row["geninfo"]["status"] = sub_status
    return row


class OnErrorModeTests(unittest.TestCase):
    def test_extract_items_shapes(self):
        rows = [{"a": 1}]
        self.assertEqual(sc.extract_items(rows), rows)
        self.assertEqual(sc.extract_items({"rows": rows}), rows)
        self.assertEqual(sc.extract_items({"result": {"rows": rows}}), rows)
        self.assertEqual(sc.extract_items({"result": rows}), rows)
        self.assertEqual(sc.extract_items({"data": [1, {"b": 2}]}), [{"b": 2}])
        self.assertEqual(sc.extract_items(None), [])
        self.assertEqual(sc.extract_items({}), [])
        with self.assertRaises(ValueError):
            sc.extract_items("oops")

    def test_id_helpers(self):
        self.assertEqual(sc.subscription_uuid({"geninfo": {"subscription_id": "g"}, "id": 7}), "g")
        self.assertEqual(sc.subscription_uuid({"uuid": "u"}), "u")
        self.assertEqual(sc.subscription_uuid({"id": 42}), "42")
        self.assertEqual(sc.subscription_uuid({}), "")
        self.assertEqual(sc.subscription_user({"context": {"user": "h1"}}), "h1")
        self.assertEqual(sc.subscription_status({"geninfo": {"status": "LOCKED"}}), "LOCKED")
        self.assertEqual(sc.demand_uuid({"uuid": "u"}), "u")
        self.assertEqual(sc.demand_uuid({"demand_id": "d"}), "d")
        self.assertEqual(sc.demand_uuid({"id": "i"}), "i")

    def test_all_demands_in_status(self):
        self.assertTrue(sc.all_demands_in_status(_err_row("s", ["ON_ERROR", "ON_ERROR"])))
        self.assertFalse(sc.all_demands_in_status(_err_row("s", ["ON_ERROR", "SUCCESS"])))
        self.assertFalse(sc.all_demands_in_status(_err_row("s", ["ON_ERROR", "ERROR"])))
        self.assertFalse(sc.all_demands_in_status(_err_row("s", [])))
        self.assertFalse(sc.all_demands_in_status({}))
        self.assertTrue(sc.all_demands_in_status(_err_row("s", ["ERROR"]), "ERROR"))

    def test_find_error_demands(self):
        subs = [
            _err_row("sub-b", ["ON_ERROR", "ON_ERROR"]),
            _err_row("sub-a", ["ON_ERROR"], sub_status="LOCKED"),
            _err_row("sub-mixed", ["ON_ERROR", "SUCCESS"]),        # une SUCCESS -> exclue
            _err_row("sub-other-user", user="h00000"),            # autre user -> exclue
            _err_row("sub-empty", []),                            # pas de demande -> exclue
            {"geninfo": {"demands": [_demand("create", "ON_ERROR")]}, "context": {"user": "h90871"}},  # pas d'uuid
        ]
        found = sc.find_error_demands(subs)
        self.assertEqual([(d.subscription_id, d.demand_id) for d in found],
                         [("sub-a", "sub-a-d0"), ("sub-b", "sub-b-d0"), ("sub-b", "sub-b-d1")])
        self.assertEqual(found[0].action, "create")
        self.assertEqual(found[0].status, "ON_ERROR")
        self.assertEqual(found[0].subscription_name, "bu003i023571")
        self.assertEqual(found[0].user, "h90871")
        self.assertEqual(found[0].create_date, "2026-01")

    def test_find_error_demands_filters(self):
        subs = [_err_row("s1", sub_status="LOCKED"), _err_row("s2", user="h00000"),
                _err_row("s3", ["FAILED", "FAILED"])]
        self.assertEqual([d.subscription_id for d in sc.find_error_demands(subs, user=None)], ["s1", "s2"])
        self.assertEqual([d.subscription_id for d in sc.find_error_demands(subs, subscription_status_filter="LOCKED")],
                         ["s1"])
        self.assertEqual([d.demand_id for d in sc.find_error_demands(subs, demand_status="FAILED")], ["s3-d0", "s3-d1"])

    def test_client_set_demand_status(self):
        client = sc.OrchestratorClient("tok")
        with mock.patch.object(client, "_request", return_value={}) as req:
            client.set_demand_status("182e47b2")
            client.set_demand_status("a/b", "DECLINED", "cleanup")
        req.assert_has_calls([
            mock.call("POST", "/state_manager/api/v1/demands/182e47b2/status",
                      {"status": "DECLINED", "reason": "to remove"}),
            mock.call("POST", "/state_manager/api/v1/demands/a%2Fb/status",
                      {"status": "DECLINED", "reason": "cleanup"}),
        ])

    def test_cli_on_error_flags(self):
        args = sc.parse_args(["--on-error", "--decline", "--yes", "--reason", "r"])
        self.assertTrue(args.on_error and args.decline and args.yes)
        self.assertEqual(args.reason, "r")
        self.assertIsNone(args.subscription_status)
        self.assertEqual(args.demand_status, "ON_ERROR")
        self.assertTrue(sc.parse_args(["--locked"]).on_error)  # alias
        self.assertEqual(sc.parse_args(["--on-error", "--subscription-status", "LOCKED"]).subscription_status,
                         "LOCKED")
        for argv in (["--decline"], ["--on-error", "--delete"]):
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                sc.parse_args(argv)

    def _fake_client(self, subs, post=None):
        client = mock.Mock()
        client.get_subscriptions.return_value = {"result": {"rows": subs}}
        client.set_demand_status.side_effect = post or (lambda *a, **k: {"ok": True})
        return client

    def _run(self, argv, client):
        with mock.patch.object(sc, "_make_client", return_value=client), \
             mock.patch("sys.stdout") as out, mock.patch("sys.stderr"):
            code = sc.main(argv)
        return code, "".join(c.args[0] for c in out.write.call_args_list)

    def test_main_dry_run_lists_without_posting(self):
        client = self._fake_client([_err_row("s1", ["ON_ERROR", "ON_ERROR"]), _err_row("s2", ["ON_ERROR", "SUCCESS"])])
        code, printed = self._run(["--on-error", "--token", "t"], client)
        self.assertEqual(code, 0)
        self.assertEqual(client.get_subscriptions.call_args.args[:3], ("cos.bucket", 100, 1))
        self.assertIn("2 souscription(s) lue(s) (user=h90871) : 1 avec toutes leurs demandes en ON_ERROR, "
                      "2 demande(s) à passer en DECLINED", printed)
        self.assertIn("demand=s1-d0", printed)
        self.assertIn("demand=s1-d1", printed)
        self.assertNotIn("s2-d0", printed)
        self.assertIn("Dry-run", printed)
        client.set_demand_status.assert_not_called()

    def test_main_json_output(self):
        client = self._fake_client([_err_row("s1")])
        code, printed = self._run(["--on-error", "--token", "t", "--json"], client)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(printed),
                         [{"subscription_id": "s1", "demand_id": "s1-d0", "action": "create", "status": "ON_ERROR"}])

    def test_main_input_file(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            json.dump({"result": {"rows": [_err_row("s1")]}}, fh)
        try:
            with mock.patch.dict(os.environ, {"ORCHESTRATOR_TOKEN": ""}), mock.patch("sys.stdout") as out:
                self.assertEqual(sc.main(["--on-error", "--input", fh.name]), 0)
            self.assertIn("demand=s1-d0", "".join(c.args[0] for c in out.write.call_args_list))
            with mock.patch.dict(os.environ, {"ORCHESTRATOR_TOKEN": ""}), mock.patch("sys.stdout"), \
                 mock.patch("sys.stderr"):
                self.assertEqual(sc.main(["--on-error", "--input", fh.name, "--decline", "--yes"]), 1)
        finally:
            os.unlink(fh.name)

    def test_main_decline_posts_each_demand(self):
        client = self._fake_client([_err_row("s1"), _err_row("s2", ["ON_ERROR", "ON_ERROR"])])
        code, _ = self._run(["--on-error", "--decline", "--yes", "--token", "t", "--reason", "bye"], client)
        self.assertEqual(code, 0)
        client.set_demand_status.assert_has_calls([
            mock.call("s1-d0", "DECLINED", "bye"), mock.call("s2-d0", "DECLINED", "bye"),
            mock.call("s2-d1", "DECLINED", "bye"),
        ])
        self.assertEqual(client.set_demand_status.call_count, 3)

    def test_main_decline_asks_confirmation(self):
        client = self._fake_client([_err_row("s1")])
        with mock.patch("builtins.input", return_value="n"):
            self.assertEqual(self._run(["--on-error", "--decline", "--token", "t"], client)[0], 0)
        client.set_demand_status.assert_not_called()
        with mock.patch("builtins.input", return_value="o"):
            self.assertEqual(self._run(["--on-error", "--decline", "--token", "t"], client)[0], 0)
        client.set_demand_status.assert_called_once_with("s1-d0", "DECLINED", "to remove")

    def test_main_decline_failure_exit_code(self):
        def post(demand_id, *a):
            if demand_id == "s1-d1":
                raise sc.OrchestratorApiError("boom", status_code=500)
            return {}
        client = self._fake_client([_err_row("s1", ["ON_ERROR", "ON_ERROR"])], post)
        code, _ = self._run(["--on-error", "--decline", "--yes", "--token", "t"], client)
        self.assertEqual(code, 3)
        self.assertEqual(client.set_demand_status.call_count, 2)

    def test_main_get_failure(self):
        client = mock.Mock()
        client.get_subscriptions.side_effect = sc.OrchestratorApiError("GET x -> HTTP 500")
        self.assertEqual(self._run(["--on-error", "--token", "t"], client)[0], 2)

    def test_main_requires_token(self):
        with mock.patch.dict(os.environ, {"ORCHESTRATOR_TOKEN": ""}), mock.patch("sys.stderr"):
            self.assertEqual(sc.main(["--on-error"]), 1)


def _make_self_signed(tmpdir: str) -> tuple[str, str, str]:
    """Génère (key.pem, cert.pem, cert.der) auto-signés pour localhost via openssl."""
    key = os.path.join(tmpdir, "key.pem")
    pem = os.path.join(tmpdir, "cert.pem")
    der = os.path.join(tmpdir, "cert.der")
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
         "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
         "-keyout", key, "-out", pem],
        check=True, capture_output=True,
    )
    subprocess.run(["openssl", "x509", "-in", pem, "-outform", "DER", "-out", der],
                   check=True, capture_output=True)
    return key, pem, der


@unittest.skipUnless(shutil.which("openssl"), "openssl absent")
class TlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp()
        cls.key, cls.pem, cls.der = _make_self_signed(cls.tmpdir)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_default_is_none(self):
        self.assertIsNone(sc.build_ssl_context())
        self.assertIsNone(sc.build_ssl_context([]))
        self.assertIsNone(sc.build_ssl_context([""]))

    def test_insecure_disables_verification(self):
        ctx = sc.build_ssl_context(insecure=True)
        self.assertFalse(ctx.check_hostname)
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)

    def test_insecure_wins_over_ca_certs(self):
        ctx = sc.build_ssl_context(["/nonexistent.cer"], insecure=True)
        self.assertEqual(ctx.verify_mode, ssl.CERT_NONE)

    def test_loads_pem_and_der(self):
        for path in (self.pem, self.der):
            ctx = sc.build_ssl_context([path])
            self.assertTrue(ctx.check_hostname)
            self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)
            subjects = [dict(x[0] for x in c["subject"]) for c in ctx.get_ca_certs()]
            self.assertIn({"commonName": "localhost"}, subjects, path)

    def test_expands_user_home(self):
        home = os.path.dirname(self.der)
        with mock.patch.dict(os.environ, {"HOME": home}):
            ctx = sc.build_ssl_context(["~/cert.der"])
        self.assertEqual(ctx.verify_mode, ssl.CERT_REQUIRED)

    def test_missing_or_invalid_file(self):
        with self.assertRaises(OSError):
            sc.build_ssl_context(["/nonexistent.cer"])
        bad = os.path.join(self.tmpdir, "bad.cer")
        with open(bad, "wb") as fh:
            fh.write(b"not a certificate")
        with self.assertRaises(ValueError):
            sc.build_ssl_context([bad])
        empty = os.path.join(self.tmpdir, "empty.cer")
        open(empty, "wb").close()
        with self.assertRaises(ValueError):
            sc.build_ssl_context([empty])

    def test_env_parsing(self):
        self.assertEqual(sc.ca_certs_from_env(None), [])
        self.assertEqual(sc.ca_certs_from_env(""), [])
        self.assertEqual(sc.ca_certs_from_env(os.pathsep.join(["a.cer", " ", "b.cer"])), ["a.cer", "b.cer"])

    def test_cli_flags(self):
        self.assertEqual(sc.parse_args(["--ca-cert", "a.cer", "b.cer", "--delete"]).ca_certs, ["a.cer", "b.cer"])
        with mock.patch.dict(os.environ, {sc.CA_CERTS_ENV: "x.cer"}):
            self.assertEqual(sc.parse_args([]).ca_certs, ["x.cer"])
        with mock.patch.dict(os.environ, {sc.CA_CERTS_ENV: ""}):
            self.assertEqual(sc.parse_args([]).ca_certs, [])
        with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
            sc.parse_args(["--ca-cert", "a.cer", "--insecure"])

    def test_end_to_end_against_self_signed_server(self):
        """Sans le CA : CERTIFICATE_VERIFY_FAILED ; avec --ca-cert (DER) : OK."""
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"result": {"rows": []}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        srv_ctx.load_cert_chain(self.pem, self.key)
        server.socket = srv_ctx.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"https://localhost:{server.server_address[1]}"
        try:
            with self.assertRaises(sc.OrchestratorApiError) as cm:
                sc.OrchestratorClient("tok", base).get_subscriptions()
            self.assertIn("CERTIFICATE_VERIFY_FAILED", str(cm.exception))
            body = sc.OrchestratorClient("tok", base, ca_certs=[self.der]).get_subscriptions()
            self.assertEqual(body, {"result": {"rows": []}})
            body = sc.OrchestratorClient("tok", base, insecure=True).get_subscriptions()
            self.assertEqual(body, {"result": {"rows": []}})
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
