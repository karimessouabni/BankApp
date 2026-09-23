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


def _sm_sub(uuid: str, status: str = "LOCKED", user: str = "h90871", name: str = "bu003i023571") -> dict:
    return {"uuid": uuid, "status": status, "name": name, "context": {"user": user}}


def _sm_demand(uuid: str, status: str = "ON_ERROR", action: str = "delete", create_date: str = "") -> dict:
    return {"uuid": uuid, "status": status, "action": action, "create_date": create_date}


class LockedModeTests(unittest.TestCase):
    def test_extract_items_shapes(self):
        rows = [{"a": 1}]
        self.assertEqual(sc.extract_items(rows), rows)
        self.assertEqual(sc.extract_items({"rows": rows}), rows)
        self.assertEqual(sc.extract_items({"result": {"rows": rows}}), rows)
        self.assertEqual(sc.extract_items({"result": rows}), rows)
        self.assertEqual(sc.extract_items({"items": rows}), rows)
        self.assertEqual(sc.extract_items({"demands": rows}), rows)
        self.assertEqual(sc.extract_items({"data": [1, {"b": 2}]}), [{"b": 2}])
        self.assertEqual(sc.extract_items(None), [])
        self.assertEqual(sc.extract_items({}), [])
        with self.assertRaises(ValueError):
            sc.extract_items("oops")

    def test_id_helpers(self):
        self.assertEqual(sc.subscription_uuid({"uuid": "u"}), "u")
        self.assertEqual(sc.subscription_uuid({"subscription_id": "s"}), "s")
        self.assertEqual(sc.subscription_uuid({"id": 42}), "42")
        self.assertEqual(sc.subscription_uuid({"geninfo": {"subscription_id": "g"}}), "g")
        self.assertEqual(sc.subscription_uuid({}), "")
        self.assertEqual(sc.subscription_user({"context": {"user": "h1"}}), "h1")
        self.assertEqual(sc.subscription_user({"owner": "h2"}), "h2")
        self.assertEqual(sc.subscription_status({"geninfo": {"status": "LOCKED"}}), "LOCKED")
        self.assertEqual(sc.demand_uuid({"demand_id": "d"}), "d")
        self.assertEqual(sc.demand_uuid({"id": "i"}), "i")

    def test_find_error_demands_filters_and_sorts(self):
        subs = [
            _sm_sub("sub-b"),
            _sm_sub("sub-a"),
            _sm_sub("sub-other-user", user="h00000"),
            _sm_sub("sub-active", status="ACTIVE"),
            {"uuid": "sub-no-user", "status": "LOCKED"},
            {"status": "LOCKED"},  # pas d'uuid -> ignorée
        ]
        demands = {
            "sub-b": {"rows": [_sm_demand("d-b2", create_date="2026-02"), _sm_demand("d-b1", create_date="2026-01"),
                               _sm_demand("d-ok", status="SUCCESS"), {"status": "ON_ERROR"}]},
            "sub-a": [_sm_demand("d-a1", action="create")],
            "sub-no-user": [_sm_demand("d-nu")],
        }
        calls = []

        def fetch(sub_id):
            calls.append(sub_id)
            return demands.get(sub_id, [])

        found = sc.find_error_demands(subs, fetch)
        self.assertEqual(calls, ["sub-b", "sub-a", "sub-no-user"])
        self.assertEqual([(d.subscription_id, d.demand_id) for d in found],
                         [("sub-a", "d-a1"), ("sub-b", "d-b1"), ("sub-b", "d-b2"), ("sub-no-user", "d-nu")])
        self.assertEqual(found[0].action, "create")
        self.assertEqual(found[0].status, "ON_ERROR")
        self.assertEqual(found[0].subscription_name, "bu003i023571")
        self.assertEqual(found[0].user, "h90871")

    def test_find_error_demands_all_users_and_custom_statuses(self):
        subs = [_sm_sub("s1", user="h00000", status="PENDING")]
        fetch = lambda _: [_sm_demand("d1", status="FAILED"), _sm_demand("d2", status="ON_ERROR")]
        found = sc.find_error_demands(subs, fetch, user=None, subscription_status_filter="PENDING",
                                      demand_status="FAILED")
        self.assertEqual([d.demand_id for d in found], ["d1"])
        self.assertEqual(sc.find_error_demands(subs, fetch), [])

    def test_client_state_manager_urls(self):
        client = sc.OrchestratorClient("tok")
        with mock.patch.object(client, "_request", return_value=[]) as req:
            client.list_subscriptions_by_status()
            client.list_subscriptions_by_status("PENDING")
            client.get_subscription_demands("0d8022cd/x")
            client.set_demand_status("182e47b2")
            client.set_demand_status("182e47b2", "DECLINED", "cleanup")
        req.assert_has_calls([
            mock.call("GET", "/state_manager/api/v1/subscription?status=LOCKED"),
            mock.call("GET", "/state_manager/api/v1/subscription?status=PENDING"),
            mock.call("GET", "/state_manager/api/v1/subscriptions/0d8022cd%2Fx/demands"),
            mock.call("POST", "/state_manager/api/v1/demands/182e47b2/status",
                      {"status": "DECLINED", "reason": "to remove"}),
            mock.call("POST", "/state_manager/api/v1/demands/182e47b2/status",
                      {"status": "DECLINED", "reason": "cleanup"}),
        ])

    def test_cli_locked_flags(self):
        args = sc.parse_args(["--locked", "--decline", "--yes", "--reason", "r"])
        self.assertTrue(args.locked and args.decline and args.yes)
        self.assertEqual(args.reason, "r")
        self.assertEqual(args.subscription_status, "LOCKED")
        self.assertEqual(args.demand_status, "ON_ERROR")
        for argv in (["--decline"], ["--locked", "--delete"], ["--locked", "--input", "x.json"]):
            with self.assertRaises(SystemExit), mock.patch("sys.stderr"):
                sc.parse_args(argv)

    def _fake_client(self, subs, demands, post=None):
        client = mock.Mock()
        client.list_subscriptions_by_status.return_value = subs
        client.get_subscription_demands.side_effect = lambda sid: demands.get(sid, [])
        client.set_demand_status.side_effect = post or (lambda *a, **k: {"ok": True})
        return client

    def test_main_locked_dry_run_lists_without_posting(self):
        client = self._fake_client([_sm_sub("s1")], {"s1": [_sm_demand("d1"), _sm_demand("d2", status="SUCCESS")]})
        with mock.patch.object(sc, "_make_client", return_value=client), \
             mock.patch("sys.stdout") as out:
            self.assertEqual(sc.main(["--locked", "--token", "t"]), 0)
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertIn("1 souscription(s) LOCKED, 1 demande(s) ON_ERROR", printed)
        self.assertIn("demand=d1", printed)
        self.assertIn("Dry-run", printed)
        client.set_demand_status.assert_not_called()

    def test_main_locked_json_output(self):
        client = self._fake_client([_sm_sub("s1")], {"s1": [_sm_demand("d1")]})
        with mock.patch.object(sc, "_make_client", return_value=client), \
             mock.patch("sys.stdout") as out:
            self.assertEqual(sc.main(["--locked", "--token", "t", "--json"]), 0)
        printed = "".join(c.args[0] for c in out.write.call_args_list)
        self.assertEqual(json.loads(printed),
                         [{"subscription_id": "s1", "demand_id": "d1", "action": "delete", "status": "ON_ERROR"}])

    def test_main_locked_decline_posts_each_demand(self):
        client = self._fake_client([_sm_sub("s1"), _sm_sub("s2")],
                                   {"s1": [_sm_demand("d1")], "s2": [_sm_demand("d2"), _sm_demand("d3")]})
        with mock.patch.object(sc, "_make_client", return_value=client), mock.patch("sys.stdout"):
            self.assertEqual(sc.main(["--locked", "--decline", "--yes", "--token", "t", "--reason", "bye"]), 0)
        client.set_demand_status.assert_has_calls([
            mock.call("d1", "DECLINED", "bye"), mock.call("d2", "DECLINED", "bye"), mock.call("d3", "DECLINED", "bye"),
        ])
        self.assertEqual(client.set_demand_status.call_count, 3)

    def test_main_locked_decline_asks_confirmation(self):
        client = self._fake_client([_sm_sub("s1")], {"s1": [_sm_demand("d1")]})
        with mock.patch.object(sc, "_make_client", return_value=client), mock.patch("sys.stdout"), \
             mock.patch("builtins.input", return_value="n"):
            self.assertEqual(sc.main(["--locked", "--decline", "--token", "t"]), 0)
        client.set_demand_status.assert_not_called()
        with mock.patch.object(sc, "_make_client", return_value=client), mock.patch("sys.stdout"), \
             mock.patch("builtins.input", return_value="o"):
            self.assertEqual(sc.main(["--locked", "--decline", "--token", "t"]), 0)
        client.set_demand_status.assert_called_once_with("d1", "DECLINED", "to remove")

    def test_main_locked_decline_failure_exit_code(self):
        def post(demand_id, *a):
            if demand_id == "d2":
                raise sc.OrchestratorApiError("boom", status_code=500)
            return {}
        client = self._fake_client([_sm_sub("s1")], {"s1": [_sm_demand("d1"), _sm_demand("d2")]}, post)
        with mock.patch.object(sc, "_make_client", return_value=client), \
             mock.patch("sys.stdout"), mock.patch("sys.stderr"):
            self.assertEqual(sc.main(["--locked", "--decline", "--yes", "--token", "t"]), 3)
        self.assertEqual(client.set_demand_status.call_count, 2)

    def test_main_locked_get_failures(self):
        client = mock.Mock()
        client.list_subscriptions_by_status.side_effect = sc.OrchestratorApiError("GET x -> HTTP 500")
        with mock.patch.object(sc, "_make_client", return_value=client), mock.patch("sys.stderr"):
            self.assertEqual(sc.main(["--locked", "--token", "t"]), 2)
        client = self._fake_client([_sm_sub("s1"), _sm_sub("s2")], {"s2": [_sm_demand("d2")]})
        client.get_subscription_demands.side_effect = (
            lambda sid: [_sm_demand("d2")] if sid == "s2" else (_ for _ in ()).throw(sc.OrchestratorApiError("nope")))
        with mock.patch.object(sc, "_make_client", return_value=client), \
             mock.patch("sys.stdout") as out, mock.patch("sys.stderr"):
            self.assertEqual(sc.main(["--locked", "--token", "t"]), 2)
        self.assertIn("demand=d2", "".join(c.args[0] for c in out.write.call_args_list))

    def test_main_locked_requires_token(self):
        with mock.patch.dict(os.environ, {"ORCHESTRATOR_TOKEN": ""}), mock.patch("sys.stderr"):
            self.assertEqual(sc.main(["--locked"]), 1)


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
