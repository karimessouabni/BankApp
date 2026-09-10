#!/usr/bin/env python3
"""
Nettoyage des souscriptions orchestrator (produit cos.bucket par défaut).

1. GET  {base}/multireader/api/v1/subscriptions?product=<product>
   -> on ne garde que les rows dont context.user == <user> (défaut: h90871),
      puis pour chacune on regarde geninfo.demands :
      la souscription est "éligible" si :
        - toutes les demandes force_clean / create / update sont en SUCCESS,
        - les demandes delete, s'il y en a, sont TOUTES en erreur
          (status != SUCCESS) : un delete réussi rend la souscription
          non éligible,
        - la liste est non vide et ne contient pas d'autre action.
2. DELETE {base}/apl/v1/subscriptions/<subscription_id>
   avec le payload {"product_branch": "main", "payload": {}}.

Usage:
    export ORCHESTRATOR_TOKEN=...            # ou --token
    python subscriptions_cleanup.py                       # liste seulement (dry-run)
    python subscriptions_cleanup.py --delete              # supprime réellement
    python subscriptions_cleanup.py --delete --yes        # sans confirmation
    python subscriptions_cleanup.py --user h12345               # autre user
    python subscriptions_cleanup.py --all-users                 # sans filtre user
    python subscriptions_cleanup.py --product cos.bucket --base-url https://...
    python subscriptions_cleanup.py --input scratch.json  # lit un JSON local au lieu du GET

Codes de sortie: 0 OK, 1 erreur args/token, 2 erreur HTTP sur le GET,
3 au moins un DELETE en échec.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Iterable

DEFAULT_BASE_URL = "https://orchestrator-gw.int.staging.echonet"
DEFAULT_PRODUCT = "cos.bucket"
DEFAULT_PRODUCT_BRANCH = "main"
DEFAULT_USER = "h90871"
DEFAULT_TIMEOUT = 60

ALLOWED_ACTIONS = frozenset({"force_clean", "create", "update"})
DELETE_ACTION = "delete"
SUCCESS_STATUS = "SUCCESS"


class OrchestratorApiError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


@dataclass
class Subscription:
    subscription_id: str
    name: str = ""
    user: str = ""
    status: str = ""
    environment: str = ""
    region: str = ""
    actions: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Filtrage
# --------------------------------------------------------------------------- #

def is_eligible(demands: Iterable[dict[str, Any]] | None) -> bool:
    """True si toutes les demandes force_clean/create/update sont en SUCCESS
    et que les éventuelles demandes delete sont toutes en erreur (!= SUCCESS)."""
    demands = list(demands or [])
    if not demands:
        return False
    for demand in demands:
        action = demand.get("action")
        succeeded = demand.get("status") == SUCCESS_STATUS
        if action in ALLOWED_ACTIONS:
            if not succeeded:
                return False
        elif action == DELETE_ACTION:
            if succeeded:
                return False
        else:
            return False
    return True


def _demand_label(demand: dict[str, Any]) -> str:
    action = demand.get("action", "?")
    status = demand.get("status", "?")
    return action if status == SUCCESS_STATUS else f"{action}({status})"


def extract_rows(body: dict[str, Any]) -> list[dict[str, Any]]:
    result = body.get("result", body)
    rows = result.get("rows", [])
    if not isinstance(rows, list):
        raise ValueError("Format inattendu: result.rows n'est pas une liste")
    return rows


def find_eligible_subscriptions(
    body: dict[str, Any], user: str | None = DEFAULT_USER
) -> list[Subscription]:
    """Retourne les souscriptions dont context.user == user (None = pas de filtre)
    et dont geninfo.demands ne contient que des force_clean / create / update
    en SUCCESS."""
    eligible: list[Subscription] = []
    for row in extract_rows(body):
        context = row.get("context") or {}
        row_user = context.get("user", "")
        if user is not None and row_user != user:
            continue
        geninfo = row.get("geninfo") or {}
        demands = geninfo.get("demands") or []
        subscription_id = geninfo.get("subscription_id")
        if not subscription_id:
            continue
        if is_eligible(demands):
            eligible.append(
                Subscription(
                    subscription_id=subscription_id,
                    name=geninfo.get("name", ""),
                    user=row_user,
                    status=geninfo.get("status", ""),
                    environment=geninfo.get("environment", ""),
                    region=geninfo.get("region", ""),
                    actions=[_demand_label(d) for d in demands],
                )
            )
    return eligible


# --------------------------------------------------------------------------- #
# Client HTTP
# --------------------------------------------------------------------------- #

class OrchestratorClient:
    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = DEFAULT_TIMEOUT,
        insecure: bool = False,
    ):
        if not token:
            raise ValueError("Bearer token manquant")
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._ssl_context: ssl.SSLContext | None = None
        if insecure:
            self._ssl_context = ssl.create_default_context()
            self._ssl_context.check_hostname = False
            self._ssl_context.verify_mode = ssl.CERT_NONE

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(
                request, timeout=self.timeout, context=self._ssl_context
            ) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            payload = _try_json(raw)
            raise OrchestratorApiError(
                f"{method} {url} -> HTTP {exc.code}: {_short(raw)}",
                status_code=exc.code,
                payload=payload,
            ) from exc
        except urllib.error.URLError as exc:
            raise OrchestratorApiError(f"{method} {url} -> {exc.reason}") from exc
        return _try_json(raw)

    def get_subscriptions(self, product: str = DEFAULT_PRODUCT) -> dict[str, Any]:
        query = urllib.parse.urlencode({"product": product})
        return self._request("GET", f"/multireader/api/v1/subscriptions?{query}")

    def delete_subscription(
        self, subscription_id: str, product_branch: str = DEFAULT_PRODUCT_BRANCH
    ) -> Any:
        """DELETE /apl/v1/subscriptions/<id> avec {"product_branch": ..., "payload": {}}."""
        path = f"/apl/v1/subscriptions/{urllib.parse.quote(subscription_id, safe='')}"
        return self._request("DELETE", path, {"product_branch": product_branch, "payload": {}})


def _try_json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def _short(raw: bytes, limit: int = 300) -> str:
    text = raw.decode("utf-8", errors="replace").strip()
    return text if len(text) <= limit else text[:limit] + "..."


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--token", default=os.environ.get("ORCHESTRATOR_TOKEN"),
                        help="bearer token (défaut: $ORCHESTRATOR_TOKEN)")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--product", default=DEFAULT_PRODUCT)
    parser.add_argument("--product-branch", default=DEFAULT_PRODUCT_BRANCH,
                        help="valeur de product_branch dans le payload DELETE")
    parser.add_argument("--user", default=DEFAULT_USER,
                        help=f"ne garder que les rows dont context.user vaut cette valeur (défaut: {DEFAULT_USER})")
    parser.add_argument("--all-users", action="store_true",
                        help="désactive le filtre sur context.user")
    parser.add_argument("--input", metavar="FILE",
                        help="lire le JSON depuis un fichier au lieu d'appeler le GET")
    parser.add_argument("--delete", action="store_true",
                        help="exécuter réellement les DELETE (sinon: liste seulement)")
    parser.add_argument("--yes", action="store_true", help="ne pas demander de confirmation")
    parser.add_argument("--insecure", action="store_true",
                        help="désactive la vérification TLS (CA interne absent)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="sortie JSON (liste des subscription_id éligibles)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.input:
        with open(args.input, encoding="utf-8") as fh:
            body = json.load(fh)
        client = None
    else:
        if not args.token:
            print("Token manquant: --token ou $ORCHESTRATOR_TOKEN", file=sys.stderr)
            return 1
        client = OrchestratorClient(args.token, args.base_url, args.timeout, args.insecure)
        try:
            body = client.get_subscriptions(args.product)
        except OrchestratorApiError as exc:
            print(f"GET échoué: {exc}", file=sys.stderr)
            return 2

    user_filter = None if args.all_users else args.user
    total_rows = len(extract_rows(body))
    eligible = find_eligible_subscriptions(body, user_filter)

    if args.as_json and not args.delete:
        print(json.dumps([s.subscription_id for s in eligible], indent=2))
        return 0

    scope = "tous users" if user_filter is None else f"user={user_filter}"
    print(f"{total_rows} souscription(s) lue(s), {len(eligible)} éligible(s) à la suppression ({scope}):")
    for sub in eligible:
        print(f"  {sub.subscription_id}  {sub.name:<16} {sub.user:<12} {sub.environment:<5} {sub.region:<7} "
              f"{sub.status:<12} demands={','.join(sub.actions)}")

    if not args.delete or not eligible:
        if eligible and not args.delete:
            print("\nDry-run: relancer avec --delete pour supprimer.")
        return 0

    if client is None:
        if not args.token:
            print("Token manquant pour les DELETE: --token ou $ORCHESTRATOR_TOKEN", file=sys.stderr)
            return 1
        client = OrchestratorClient(args.token, args.base_url, args.timeout, args.insecure)

    if not args.yes:
        answer = input(f"\nSupprimer ces {len(eligible)} souscription(s) ? [y/N] ").strip().lower()
        if answer not in ("y", "yes", "o", "oui"):
            print("Annulé.")
            return 0

    failures = 0
    for sub in eligible:
        try:
            response = client.delete_subscription(sub.subscription_id, args.product_branch)
            print(f"DELETE {sub.subscription_id} ({sub.name}) -> OK {_summ(response)}")
        except OrchestratorApiError as exc:
            failures += 1
            print(f"DELETE {sub.subscription_id} ({sub.name}) -> ERREUR {exc}", file=sys.stderr)

    print(f"\n{len(eligible) - failures} supprimée(s), {failures} en échec.")
    return 3 if failures else 0


def _summ(response: Any) -> str:
    if response is None:
        return ""
    text = response if isinstance(response, str) else json.dumps(response)
    return text if len(text) <= 120 else text[:120] + "..."


if __name__ == "__main__":
    sys.exit(main())
