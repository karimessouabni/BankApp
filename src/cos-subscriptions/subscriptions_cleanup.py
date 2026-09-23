#!/usr/bin/env python3
"""
Nettoyage des souscriptions orchestrator (produit cos.bucket par défaut).

1. GET  {base}/multireader/api/v1/subscriptions?page=<n>&size=100, page par page
   jusqu'à la dernière (page incomplète, vide, ou total_pages/total atteint)
   -> on ne garde que les rows dont geninfo.product == <product> et
      context.user == <user> (défaut: h90871),
      puis pour chacune on regarde geninfo.demands :
      la souscription est "éligible" si :
        - toutes les demandes force_clean / create / update sont en SUCCESS,
        - les demandes delete, s'il y en a, sont TOUTES en erreur
          (status != SUCCESS) : un delete réussi rend la souscription
          non éligible,
        - la liste est non vide et ne contient pas d'autre action.
2. Pour chaque souscription éligible :
   - sans delete en échec : DELETE {base}/apl/v1/subscriptions/<subscription_id>
     avec le payload {"product_branch": "main", "payload": {}} ;
   - avec delete(s) en échec : on relance la demande existante plutôt que
     d'en créer une nouvelle :
       GET  {base}/api/v1/demands/<demand_uuid>
       -> names des processes dont status == ERROR
       POST {base}/api/v1/demands/<demand_uuid>/retry
            {"tasks": [<names>], "retry_non_failed_tasks": false}

Mode "on-error" (--on-error) : décliner les demandes des souscriptions dont
TOUTES les demandes sont en ON_ERROR.
1. même listing paginé que le mode delete, filtré côté script sur
   geninfo.product == <product> et context.user == <user>
2. est éligible une souscription dont geninfo.demands est non vide et dont
   chaque demande a status == ON_ERROR ; les demandes à décliner sont celles-là
   (leur uuid est celui de la demande)
3. Avec --decline, pour chaque demande retenue :
   POST {base}/state_manager/api/v1/demands/<uuid>/status
        {"status": "DECLINED", "reason": "to remove"}

Usage:
    export ORCHESTRATOR_TOKEN=...            # ou --token
    python subscriptions_cleanup.py                       # liste seulement (dry-run)
    python subscriptions_cleanup.py --delete              # supprime / relance réellement
    python subscriptions_cleanup.py --delete --yes        # sans confirmation
    python subscriptions_cleanup.py --user h12345               # autre user
    python subscriptions_cleanup.py --all-users                 # sans filtre user
    python subscriptions_cleanup.py --product cos.bucket --base-url https://...
    python subscriptions_cleanup.py --page-size 50 --first-page 0   # pagination
    python subscriptions_cleanup.py --input scratch.json  # lit un JSON local au lieu du GET
    python subscriptions_cleanup.py --on-error                  # liste les demandes à décliner (dry-run)
    python subscriptions_cleanup.py --on-error --decline        # POST DECLINED sur chacune
    python subscriptions_cleanup.py --on-error --decline --yes --reason "cleanup sprint 12"
    python subscriptions_cleanup.py --on-error --subscription-status LOCKED   # restreint aux LOCKED

TLS (certificat interne BNPP, sinon "CERTIFICATE_VERIFY_FAILED: self-signed
certificate in certificate chain") :
    python subscriptions_cleanup.py --ca-cert ~/Root-Certificats-Internes/*.cer
    export ORCHESTRATOR_CA_CERTS=~/Root-Certificats-Internes/2014-2044\ BNPP\ Root.cer
    python subscriptions_cleanup.py --insecure   # dernier recours : pas de vérification

Codes de sortie: 0 OK, 1 erreur args/token, 2 erreur HTTP sur le GET,
3 au moins un DELETE / retry / decline en échec.
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
from typing import Any, Callable, Iterable

DEFAULT_BASE_URL = "https://orchestrator-gw.int.staging.echonet"
DEFAULT_PRODUCT = "cos.bucket"
DEFAULT_PRODUCT_BRANCH = "main"
DEFAULT_USER = "h90871"
DEFAULT_TIMEOUT = 60
DEFAULT_PAGE_SIZE = 100
DEFAULT_FIRST_PAGE = 1
MAX_PAGES = 10_000
CA_CERTS_ENV = "ORCHESTRATOR_CA_CERTS"  # chemins séparés par os.pathsep (":" sur macOS/Linux)

STATE_MANAGER_PREFIX = "/state_manager/api/v1"
DEMAND_ON_ERROR_STATUS = "ON_ERROR"
DECLINED_STATUS = "DECLINED"
DEFAULT_DECLINE_REASON = "to remove"

ALLOWED_ACTIONS = frozenset({"force_clean", "create", "update"})
DELETE_ACTION = "delete"
SUCCESS_STATUS = "SUCCESS"
PROCESS_ERROR_STATUS = "ERROR"


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
    failed_delete_demand_ids: list[str] = field(default_factory=list)

    @property
    def needs_retry(self) -> bool:
        return bool(self.failed_delete_demand_ids)


@dataclass
class ErrorDemand:
    """Demande d'une souscription dont toutes les demandes sont en erreur (mode --on-error)."""
    subscription_id: str
    demand_id: str
    subscription_name: str = ""
    user: str = ""
    action: str = ""
    status: str = ""
    create_date: str = ""


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


def failed_delete_demand_ids(demands: Iterable[dict[str, Any]]) -> list[str]:
    """uuid des demandes delete non SUCCESS, dans l'ordre de create_date."""
    failed = [
        d for d in demands
        if d.get("action") == DELETE_ACTION and d.get("status") != SUCCESS_STATUS and d.get("uuid")
    ]
    failed.sort(key=lambda d: d.get("create_date") or "")
    return [d["uuid"] for d in failed]


def failed_process_names(demand: dict[str, Any]) -> list[str]:
    """names des processes en ERROR dans la réponse GET /api/v1/demands/<uuid>."""
    processes = demand.get("processes") or []
    return [
        p["name"] for p in processes
        if p.get("status") == PROCESS_ERROR_STATUS and p.get("name")
    ]


def extract_rows(body: dict[str, Any]) -> list[dict[str, Any]]:
    result = body.get("result", body)
    rows = result.get("rows", [])
    if not isinstance(rows, list):
        raise ValueError("Format inattendu: result.rows n'est pas une liste")
    return rows


def extract_items(body: Any) -> list[dict[str, Any]]:
    """Liste d'objets depuis une réponse state_manager : liste brute, ou dict
    avec result/rows/items/data/demands/subscriptions."""
    if body is None:
        return []
    if isinstance(body, list):
        return [x for x in body if isinstance(x, dict)]
    if not isinstance(body, dict):
        raise ValueError(f"Format inattendu: {type(body).__name__}")
    for key in ("rows", "items", "data", "demands", "subscriptions"):
        value = body.get(key)
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    result = body.get("result")
    if isinstance(result, (list, dict)):
        return extract_items(result)
    return []


def _first(mapping: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value:
            return str(value)
    return ""


def subscription_uuid(row: dict[str, Any]) -> str:
    """uuid de la souscription : geninfo.subscription_id (rows multireader),
    sinon uuid / subscription_id / id au premier niveau."""
    geninfo = row.get("geninfo") or {}
    return _first(geninfo, "subscription_id", "uuid") or _first(row, "uuid", "subscription_id", "id")


def subscription_user(row: dict[str, Any]) -> str:
    context = row.get("context") or {}
    return _first(context, "user") or _first(row, "user", "owner", "requester")


def subscription_status(row: dict[str, Any]) -> str:
    geninfo = row.get("geninfo") or {}
    return _first(row, "status") or _first(geninfo, "status")


def subscription_name(row: dict[str, Any]) -> str:
    geninfo = row.get("geninfo") or {}
    return _first(row, "name") or _first(geninfo, "name")


def demand_uuid(demand: dict[str, Any]) -> str:
    return _first(demand, "uuid", "demand_id", "id")


def all_demands_in_status(row: dict[str, Any], status: str = DEMAND_ON_ERROR_STATUS) -> bool:
    """True si geninfo.demands est non vide et que chaque demande a ce status."""
    demands = (row.get("geninfo") or {}).get("demands") or []
    return bool(demands) and all(d.get("status") == status for d in demands)


def find_error_demands(
    subscriptions: Iterable[dict[str, Any]],
    user: str | None = DEFAULT_USER,
    demand_status: str = DEMAND_ON_ERROR_STATUS,
    subscription_status_filter: str | None = None,
) -> list[ErrorDemand]:
    """Souscriptions du listing dont TOUTES les demandes (geninfo.demands) sont en
    demand_status, filtrées sur context.user == user (si user n'est pas None) et
    sur geninfo.status == subscription_status_filter (si fourni). Retourne leurs
    demandes, triées par souscription puis create_date."""
    found: list[ErrorDemand] = []
    for row in subscriptions:
        sub_id = subscription_uuid(row)
        if not sub_id:
            continue
        row_user = subscription_user(row)
        if user is not None and row_user != user:
            continue
        if subscription_status_filter and subscription_status(row) != subscription_status_filter:
            continue
        if not all_demands_in_status(row, demand_status):
            continue
        for demand in (row.get("geninfo") or {}).get("demands") or []:
            demand_id = demand_uuid(demand)
            if not demand_id:
                continue
            found.append(ErrorDemand(
                subscription_id=sub_id,
                demand_id=demand_id,
                subscription_name=subscription_name(row),
                user=row_user,
                action=_first(demand, "action"),
                status=str(demand.get("status", "")),
                create_date=_first(demand, "create_date", "created_at"),
            ))
    found.sort(key=lambda d: (d.subscription_id, d.create_date, d.demand_id))
    return found


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def total_pages_hint(body: Any, size: int) -> int | None:
    """Nombre total de pages annoncé par la réponse (total_pages / totalPages,
    ou total / total_count / totalElements / count divisé par size), sinon None."""
    if not isinstance(body, dict):
        return None
    candidates = [body]
    result = body.get("result")
    if isinstance(result, dict):
        candidates.append(result)
    for key in ("page", "pagination", "meta"):
        for c in list(candidates):
            nested = c.get(key)
            if isinstance(nested, dict):
                candidates.append(nested)
    for c in candidates:
        for key in ("total_pages", "totalPages", "pages", "page_count", "pageCount"):
            n = _as_int(c.get(key))
            if n is not None:
                return max(n, 0)
    for c in candidates:
        for key in ("total", "total_count", "totalCount", "totalElements", "total_elements", "count"):
            n = _as_int(c.get(key))
            if n is not None:
                return max(-(-n // size), 0) if size > 0 else None
    return None


def iterate_pages(
    fetch_page: Callable[[int, int], Any],
    size: int = DEFAULT_PAGE_SIZE,
    first_page: int = DEFAULT_FIRST_PAGE,
    max_pages: int = MAX_PAGES,
    progress: Callable[[int, int], None] | None = None,
) -> list[dict[str, Any]]:
    """Appelle fetch_page(page, size) depuis first_page et concatène les rows.

    Arrêt : page vide, page incomplète (< size), total_pages atteint, page
    identique à la précédente (API qui ignore ?page=), ou max_pages."""
    rows: list[dict[str, Any]] = []
    previous_keys: list[str] | None = None
    for index in range(max_pages):
        page = first_page + index
        body = fetch_page(page, size)
        page_rows = extract_rows(body) if isinstance(body, dict) else extract_items(body)
        if progress:
            progress(page, len(page_rows))
        if not page_rows:
            break
        keys = [subscription_uuid(r) for r in page_rows]
        if keys == previous_keys:
            break
        rows.extend(page_rows)
        hint = total_pages_hint(body, size)
        if hint is not None and index + 1 >= hint:
            break
        if len(page_rows) < size:
            break
        previous_keys = keys
    return rows


def dedupe_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Supprime les doublons de subscription uuid (chevauchement de pages)."""
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        key = subscription_uuid(row)
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        out.append(row)
    return out


def filter_product(rows: Iterable[dict[str, Any]], product: str | None) -> list[dict[str, Any]]:
    """Garde les rows dont geninfo.product == product (rows sans product conservées)."""
    if not product:
        return list(rows)
    return [r for r in rows if (r.get("geninfo") or {}).get("product", product) == product]


def find_eligible_subscriptions(
    body: dict[str, Any], user: str | None = DEFAULT_USER
) -> list[Subscription]:
    """Retourne les souscriptions dont context.user == user (None = pas de filtre)
    et dont geninfo.demands respecte is_eligible()."""
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
                    failed_delete_demand_ids=failed_delete_demand_ids(demands),
                )
            )
    return eligible


# --------------------------------------------------------------------------- #
# Client HTTP
# --------------------------------------------------------------------------- #

def _load_ca_cert(context: ssl.SSLContext, path: str) -> None:
    """Ajoute un certificat CA (fichier .cer/.crt/.pem, encodé PEM ou DER) au contexte."""
    with open(path, "rb") as fh:
        data = fh.read()
    if not data.strip():
        raise ValueError(f"certificat vide: {path}")
    try:
        if b"-----BEGIN" in data:
            context.load_verify_locations(cadata=data.decode("ascii"))
        else:
            context.load_verify_locations(cadata=data)
    except (ssl.SSLError, UnicodeDecodeError, ValueError) as exc:
        raise ValueError(f"certificat illisible: {path} ({exc})") from exc


def build_ssl_context(ca_certs: Iterable[str] | None = None, insecure: bool = False) -> ssl.SSLContext | None:
    """Contexte TLS pour urlopen.

    - insecure : aucune vérification (check_hostname=False, CERT_NONE) ;
    - ca_certs : CA système + chaque fichier (PEM ou DER), typiquement les
      "Root-Certificats-Internes" BNPP ;
    - sinon None : comportement par défaut de urllib (CA système / $SSL_CERT_FILE).
    """
    if insecure:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        return context
    paths = [os.path.expanduser(p) for p in (ca_certs or []) if p]
    if not paths:
        return None
    context = ssl.create_default_context()
    for path in paths:
        _load_ca_cert(context, path)
    return context


def ca_certs_from_env(value: str | None) -> list[str]:
    """Découpe $ORCHESTRATOR_CA_CERTS (séparateur os.pathsep) en liste de chemins."""
    if not value:
        return []
    return [p.strip() for p in value.split(os.pathsep) if p.strip()]


class OrchestratorClient:
    def __init__(
        self,
        token: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout: int = DEFAULT_TIMEOUT,
        insecure: bool = False,
        ca_certs: Iterable[str] | None = None,
    ):
        if not token:
            raise ValueError("Bearer token manquant")
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._ssl_context = build_ssl_context(ca_certs, insecure)

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

    def get_subscriptions_page(self, page: int, size: int = DEFAULT_PAGE_SIZE) -> Any:
        """GET /multireader/api/v1/subscriptions?page=<page>&size=<size>."""
        query = urllib.parse.urlencode({"page": page, "size": size})
        return self._request("GET", f"/multireader/api/v1/subscriptions?{query}")

    def get_subscriptions(
        self,
        product: str | None = DEFAULT_PRODUCT,
        page_size: int = DEFAULT_PAGE_SIZE,
        first_page: int = DEFAULT_FIRST_PAGE,
        progress: Callable[[int, int], None] | None = None,
    ) -> dict[str, Any]:
        """Parcourt toutes les pages et retourne {"result": {"rows": [...]}} avec
        toutes les souscriptions (dédoublonnées), filtrées sur geninfo.product."""
        rows = iterate_pages(self.get_subscriptions_page, page_size, first_page, progress=progress)
        return {"result": {"rows": filter_product(dedupe_rows(rows), product)}}

    def delete_subscription(
        self, subscription_id: str, product_branch: str = DEFAULT_PRODUCT_BRANCH
    ) -> Any:
        """DELETE /apl/v1/subscriptions/<id> avec {"product_branch": ..., "payload": {}}."""
        path = f"/apl/v1/subscriptions/{urllib.parse.quote(subscription_id, safe='')}"
        return self._request("DELETE", path, {"product_branch": product_branch, "payload": {}})

    def get_demand(self, demand_id: str) -> dict[str, Any]:
        """GET /api/v1/demands/<uuid>."""
        return self._request("GET", f"/api/v1/demands/{urllib.parse.quote(demand_id, safe='')}")

    def retry_demand(self, demand_id: str, tasks: list[str], retry_non_failed_tasks: bool = False) -> Any:
        """POST /api/v1/demands/<uuid>/retry avec {"tasks": [...], "retry_non_failed_tasks": false}."""
        path = f"/api/v1/demands/{urllib.parse.quote(demand_id, safe='')}/retry"
        return self._request("POST", path, {"tasks": tasks, "retry_non_failed_tasks": retry_non_failed_tasks})

    # --- state_manager (mode --on-error) ---

    def set_demand_status(self, demand_id: str, status: str = DECLINED_STATUS,
                          reason: str = DEFAULT_DECLINE_REASON) -> Any:
        """POST /state_manager/api/v1/demands/<demand_id>/status {"status": ..., "reason": ...}."""
        path = f"{STATE_MANAGER_PREFIX}/demands/{urllib.parse.quote(demand_id, safe='')}/status"
        return self._request("POST", path, {"status": status, "reason": reason})

    def retry_failed_delete(self, demand_id: str) -> list[str]:
        """GET la demande, relance ses processes en ERROR. Retourne les tasks relancées
        (liste vide si aucun process en ERROR : rien n'est envoyé)."""
        tasks = failed_process_names(self.get_demand(demand_id))
        if tasks:
            self.retry_demand(demand_id, tasks)
        return tasks


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
    parser.add_argument("--product", default=DEFAULT_PRODUCT,
                        help=f"ne garder que les rows dont geninfo.product vaut cette valeur (défaut: {DEFAULT_PRODUCT})")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE,
                        help=f"paramètre size de la pagination (défaut: {DEFAULT_PAGE_SIZE})")
    parser.add_argument("--first-page", type=int, default=DEFAULT_FIRST_PAGE,
                        help=f"numéro de la première page (défaut: {DEFAULT_FIRST_PAGE})")
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
    on_error = parser.add_argument_group("mode on-error (demandes à décliner)")
    on_error.add_argument("--on-error", "--locked", action="store_true", dest="on_error",
                          help="lister les demandes des souscriptions dont toutes les demandes sont en ON_ERROR")
    on_error.add_argument("--decline", action="store_true",
                          help=f"avec --on-error : POST status={DECLINED_STATUS} sur chaque demande listée")
    on_error.add_argument("--reason", default=DEFAULT_DECLINE_REASON,
                          help=f"reason envoyée avec le POST status (défaut: {DEFAULT_DECLINE_REASON!r})")
    on_error.add_argument("--subscription-status", default=None,
                          help="ne garder que les souscriptions ayant ce geninfo.status (ex: LOCKED ; défaut: tous)")
    on_error.add_argument("--demand-status", default=DEMAND_ON_ERROR_STATUS,
                          help=f"status que doivent avoir toutes les demandes (défaut: {DEMAND_ON_ERROR_STATUS})")
    tls = parser.add_mutually_exclusive_group()
    tls.add_argument("--ca-cert", nargs="+", metavar="FILE", dest="ca_certs",
                     default=ca_certs_from_env(os.environ.get(CA_CERTS_ENV)),
                     help="certificat(s) CA interne(s) à ajouter aux CA système, .cer/.pem en PEM ou DER "
                          f"(défaut: ${CA_CERTS_ENV}, chemins séparés par '{os.pathsep}')")
    tls.add_argument("--insecure", action="store_true",
                     help="désactive la vérification TLS (dernier recours)")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--json", action="store_true", dest="as_json",
                        help="sortie JSON (liste des subscription_id éligibles)")
    args = parser.parse_args(argv)
    if args.page_size < 1:
        parser.error("--page-size doit être >= 1")
    if args.decline and not args.on_error:
        parser.error("--decline nécessite --on-error")
    if args.on_error and args.delete:
        parser.error("--on-error est incompatible avec --delete")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.on_error:
        return main_on_error(args)

    if args.input:
        with open(args.input, encoding="utf-8") as fh:
            body = json.load(fh)
        client = None
    else:
        if not args.token:
            print("Token manquant: --token ou $ORCHESTRATOR_TOKEN", file=sys.stderr)
            return 1
        try:
            client = _make_client(args)
        except (OSError, ValueError) as exc:
            print(f"Certificat CA invalide: {exc}", file=sys.stderr)
            return 1
        try:
            body = _fetch_all_subscriptions(client, args)
        except OrchestratorApiError as exc:
            print(f"GET échoué: {exc}", file=sys.stderr)
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                print(f"Astuce: passer le CA interne avec --ca-cert <fichier.cer> (ou ${CA_CERTS_ENV}).",
                      file=sys.stderr)
            return 2

    user_filter = None if args.all_users else args.user
    total_rows = len(extract_rows(body))
    eligible = find_eligible_subscriptions(body, user_filter)

    if args.as_json and not args.delete:
        print(json.dumps([s.subscription_id for s in eligible], indent=2))
        return 0

    scope = "tous users" if user_filter is None else f"user={user_filter}"
    to_retry = [s for s in eligible if s.needs_retry]
    to_delete = [s for s in eligible if not s.needs_retry]
    print(f"{total_rows} souscription(s) lue(s), {len(eligible)} éligible(s) ({scope}): "
          f"{len(to_delete)} à supprimer, {len(to_retry)} delete à relancer")
    for sub in eligible:
        plan = f"RETRY {','.join(sub.failed_delete_demand_ids)}" if sub.needs_retry else "DELETE"
        print(f"  {sub.subscription_id}  {sub.name:<16} {sub.user:<12} {sub.environment:<5} {sub.region:<7} "
              f"{sub.status:<12} demands={','.join(sub.actions)}  -> {plan}")

    if not args.delete or not eligible:
        if eligible and not args.delete:
            print("\nDry-run: relancer avec --delete pour exécuter.")
        return 0

    if client is None:
        if not args.token:
            print("Token manquant pour les DELETE/retry: --token ou $ORCHESTRATOR_TOKEN", file=sys.stderr)
            return 1
        try:
            client = _make_client(args)
        except (OSError, ValueError) as exc:
            print(f"Certificat CA invalide: {exc}", file=sys.stderr)
            return 1

    if not args.yes:
        answer = input(f"\nExécuter {len(to_delete)} DELETE et {len(to_retry)} retry ? [y/N] ").strip().lower()
        if answer not in ("y", "yes", "o", "oui"):
            print("Annulé.")
            return 0

    failures = 0
    for sub in eligible:
        if sub.needs_retry:
            for demand_id in sub.failed_delete_demand_ids:
                try:
                    tasks = client.retry_failed_delete(demand_id)
                except OrchestratorApiError as exc:
                    failures += 1
                    print(f"RETRY {demand_id} ({sub.name}) -> ERREUR {exc}", file=sys.stderr)
                    continue
                if tasks:
                    print(f"RETRY {demand_id} ({sub.name}) -> OK tasks={','.join(tasks)}")
                else:
                    print(f"RETRY {demand_id} ({sub.name}) -> ignoré, aucun process en {PROCESS_ERROR_STATUS}")
            continue
        try:
            response = client.delete_subscription(sub.subscription_id, args.product_branch)
            print(f"DELETE {sub.subscription_id} ({sub.name}) -> OK {_summ(response)}")
        except OrchestratorApiError as exc:
            failures += 1
            print(f"DELETE {sub.subscription_id} ({sub.name}) -> ERREUR {exc}", file=sys.stderr)

    print(f"\n{len(eligible) - failures} traitée(s), {failures} en échec.")
    return 3 if failures else 0


def main_on_error(args: argparse.Namespace) -> int:
    """Mode --on-error : liste (et avec --decline, décline) les demandes des
    souscriptions dont toutes les demandes sont en ON_ERROR."""
    client: OrchestratorClient | None = None
    if args.input:
        with open(args.input, encoding="utf-8") as fh:
            body = json.load(fh)
    else:
        if not args.token:
            print("Token manquant: --token ou $ORCHESTRATOR_TOKEN", file=sys.stderr)
            return 1
        try:
            client = _make_client(args)
        except (OSError, ValueError) as exc:
            print(f"Certificat CA invalide: {exc}", file=sys.stderr)
            return 1
        try:
            body = _fetch_all_subscriptions(client, args)
        except OrchestratorApiError as exc:
            print(f"GET échoué: {exc}", file=sys.stderr)
            if "CERTIFICATE_VERIFY_FAILED" in str(exc):
                print(f"Astuce: passer le CA interne avec --ca-cert <fichier.cer> (ou ${CA_CERTS_ENV}).",
                      file=sys.stderr)
            return 2

    user_filter = None if args.all_users else args.user
    subscriptions = extract_rows(body)
    demands = find_error_demands(subscriptions, user_filter, args.demand_status, args.subscription_status)
    eligible_subs = {d.subscription_id for d in demands}

    if args.as_json and not args.decline:
        print(json.dumps([{"subscription_id": d.subscription_id, "demand_id": d.demand_id,
                           "action": d.action, "status": d.status} for d in demands], indent=2))
        return 0

    scope = "tous users" if user_filter is None else f"user={user_filter}"
    if args.subscription_status:
        scope += f", status={args.subscription_status}"
    print(f"{len(subscriptions)} souscription(s) lue(s) ({scope}) : {len(eligible_subs)} avec toutes leurs "
          f"demandes en {args.demand_status}, {len(demands)} demande(s) à passer en {DECLINED_STATUS}")
    for d in demands:
        print(f"  {d.subscription_id}  {d.subscription_name:<16} {d.user:<12} "
              f"demand={d.demand_id} {d.action:<12} {d.status:<10} {d.create_date}"
              f"  -> {DECLINED_STATUS} ({args.reason})")

    if not args.decline or not demands:
        if demands and not args.decline:
            print("\nDry-run: relancer avec --on-error --decline pour exécuter.")
        return 0

    if client is None:
        if not args.token:
            print("Token manquant pour les POST status: --token ou $ORCHESTRATOR_TOKEN", file=sys.stderr)
            return 1
        try:
            client = _make_client(args)
        except (OSError, ValueError) as exc:
            print(f"Certificat CA invalide: {exc}", file=sys.stderr)
            return 1

    if not args.yes:
        answer = input(f"\nPasser {len(demands)} demande(s) en {DECLINED_STATUS} ? [y/N] ").strip().lower()
        if answer not in ("y", "yes", "o", "oui"):
            print("Annulé.")
            return 0

    failures = 0
    for d in demands:
        label = d.subscription_name or d.subscription_id
        try:
            response = client.set_demand_status(d.demand_id, DECLINED_STATUS, args.reason)
            print(f"DECLINE {d.demand_id} ({label}) -> OK {_summ(response)}")
        except OrchestratorApiError as exc:
            failures += 1
            print(f"DECLINE {d.demand_id} ({label}) -> ERREUR {exc}", file=sys.stderr)

    print(f"\n{len(demands) - failures} déclinée(s), {failures} en échec.")
    return 3 if failures else 0


def _fetch_all_subscriptions(client: OrchestratorClient, args: argparse.Namespace) -> dict[str, Any]:
    def progress(page: int, count: int) -> None:
        print(f"page {page}: {count} row(s)", file=sys.stderr)

    return client.get_subscriptions(args.product, args.page_size, args.first_page, progress)


def _make_client(args: argparse.Namespace) -> OrchestratorClient:
    return OrchestratorClient(args.token, args.base_url, args.timeout,
                              insecure=args.insecure, ca_certs=args.ca_certs)


def _summ(response: Any) -> str:
    if response is None:
        return ""
    text = response if isinstance(response, str) else json.dumps(response)
    return text if len(text) <= 120 else text[:120] + "..."


if __name__ == "__main__":
    sys.exit(main())
