#!/usr/bin/env bash
#
# Télécharge un objet depuis un bucket IBM COS (endpoint direct/privé).
# Même architecture que empty-bucket-script.sh / multipart-cleaning.sh :
# token IAM récupéré une fois (ou fourni via --token), wrapper cos(),
# body dans un fichier temp, HTTP code capturé séparément, bash 3.2 OK.
#
# Usage:
#   export IBM_API_KEY=...            # ou --apikey, ou --token / $COS_TOKEN
#   ./cos-download.sh --bucket bu002i011983 --key "chemin/fichier_1.3_20260821.csv"
#   ./cos-download.sh --bucket bu002i011983 --key "..." --out ~/Downloads/fichier.csv
#   ./cos-download.sh --bucket bu002i011983 --key "..." --resume
#   ./cos-download.sh --bucket bu002i011983 --key "..." --dry-run   # HEAD seulement
#
# Options:
#   --bucket <name>     bucket COS (obligatoire)
#   --key <key>         clé de l'objet, non encodée (obligatoire)
#   --out <path>        fichier de sortie (défaut: basename de la clé, dans .)
#   --host <host>       défaut: s3.direct.eu-fr2.cloud-object-storage.appdomain.cloud:443
#   --apikey <key>      API key IAM (défaut: $IBM_API_KEY)
#   --token <token>     bearer token déjà obtenu (défaut: $COS_TOKEN) — bypass IAM
#   --iam <url>         défaut: https://private.iam.cloud.ibm.com/identity/token
#   --resume            reprend un téléchargement partiel (curl -C -)
#   --insecure          ajoute -k à curl (CA interne absent du trust store)
#   --dry-run           HEAD uniquement, affiche taille/ETag, ne télécharge rien
#
# Codes de sortie: 0 OK, 1 erreur args/IAM, 2 objet introuvable/403, 3 échec téléchargement, 4 intégrité KO

set -eu

BUCKET=""
KEY=""
OUT=""
HOST="s3.direct.eu-fr2.cloud-object-storage.appdomain.cloud:443"
APIKEY="${IBM_API_KEY:-}"
TOKEN="${COS_TOKEN:-}"
IAM_URL="https://private.iam.cloud.ibm.com/identity/token"
RESUME=0
INSECURE=0
DRY_RUN=0

while [ $# -gt 0 ]; do
  case "$1" in
    --bucket)   BUCKET="$2"; shift 2 ;;
    --key)      KEY="$2"; shift 2 ;;
    --out)      OUT="$2"; shift 2 ;;
    --host)     HOST="$2"; shift 2 ;;
    --apikey)   APIKEY="$2"; shift 2 ;;
    --token)    TOKEN="$2"; shift 2 ;;
    --iam)      IAM_URL="$2"; shift 2 ;;
    --resume)   RESUME=1; shift ;;
    --insecure) INSECURE=1; shift ;;
    --dry-run)  DRY_RUN=1; shift ;;
    -h|--help)  sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "Option inconnue: $1" >&2; exit 1 ;;
  esac
done

[ -n "$BUCKET" ] || { echo "ERREUR: --bucket obligatoire" >&2; exit 1; }
[ -n "$KEY" ]    || { echo "ERREUR: --key obligatoire" >&2; exit 1; }
[ -n "$APIKEY" ] || [ -n "$TOKEN" ] || { echo "ERREUR: --apikey / IBM_API_KEY ou --token / COS_TOKEN requis" >&2; exit 1; }
command -v python3 >/dev/null || { echo "ERREUR: python3 requis (urlencode)" >&2; exit 1; }

[ -n "$OUT" ] || OUT="$(basename "$KEY")"

WORKDIR="$(mktemp -d)"
BODY_FILE="$WORKDIR/body"
HDR_FILE="$WORKDIR/headers"
trap 'rm -rf "$WORKDIR"' EXIT

CURL_OPTS="-sS"
[ "$INSECURE" -eq 1 ] && CURL_OPTS="$CURL_OPTS -k"

log() { printf '%s %s\n' "$(date '+%H:%M:%S')" "$*" >&2; }

urlencode_key() {
  python3 -c 'import sys, urllib.parse; print(urllib.parse.quote(sys.argv[1], safe="/"))' "$1"
}

# ---------------------------------------------------------------------------
# IAM
# ---------------------------------------------------------------------------
get_token() {
  # shellcheck disable=SC2086
  HTTP_CODE=$(curl $CURL_OPTS -o "$BODY_FILE" -w '%{http_code}' \
    -X POST "$IAM_URL" \
    -H 'Content-Type: application/x-www-form-urlencoded' \
    -H 'Accept: application/json' \
    --data-urlencode 'grant_type=urn:ibm:params:oauth:grant-type:apikey' \
    --data-urlencode "apikey=$APIKEY") || { log "IAM injoignable ($IAM_URL)"; exit 1; }
  if [ "$HTTP_CODE" != "200" ]; then
    log "IAM HTTP $HTTP_CODE : $(head -c 300 "$BODY_FILE")"
    exit 1
  fi
  TOKEN=$(python3 -c 'import sys, json; print(json.load(open(sys.argv[1]))["access_token"])' "$BODY_FILE")
  log "Token IAM obtenu"
}

[ -n "$TOKEN" ] || get_token

# ---------------------------------------------------------------------------
# cos() : appel sans $() pour conserver HTTP_CODE dans le shell courant.
# Usage: cos <METHOD> <path-encodé> [options curl supplémentaires...]
# ---------------------------------------------------------------------------
cos() {
  local method="$1"; local path="$2"; shift 2
  # shellcheck disable=SC2086
  HTTP_CODE=$(curl $CURL_OPTS -o "$BODY_FILE" -D "$HDR_FILE" -w '%{http_code}' \
    -X "$method" "https://${HOST}/${BUCKET}/${path}" \
    -H "Authorization: Bearer $TOKEN" "$@") || { log "curl échec ($method $path)"; return 1; }
  return 0
}

header() {  # header <nom> — lit dans HDR_FILE, insensible à la casse, sans CR
  awk -v h="$(printf '%s' "$1" | tr 'A-Z' 'a-z')" -F': ' \
    'tolower($1)==h {sub(/\r$/,""); print $2}' "$HDR_FILE" | tail -n1
}

ENC_KEY="$(urlencode_key "$KEY")"

# ---------------------------------------------------------------------------
# 1. HEAD : existence, taille, ETag
# ---------------------------------------------------------------------------
cos HEAD "$ENC_KEY" -I || exit 2
case "$HTTP_CODE" in
  200) ;;
  404) log "Objet introuvable: s3://$BUCKET/$KEY"; exit 2 ;;
  403) log "403 sur HEAD — rôle IAM insuffisant (Reader minimum) ou firewall/CBR"; exit 2 ;;
  *)   log "HEAD HTTP $HTTP_CODE"; head -c 300 "$HDR_FILE" >&2; exit 2 ;;
esac

SIZE="$(header Content-Length)"
ETAG="$(header ETag | tr -d '"')"
log "Objet: s3://$BUCKET/$KEY"
log "Taille: ${SIZE:-?} octets — ETag: ${ETAG:-?}"

if [ "$DRY_RUN" -eq 1 ]; then
  log "dry-run : aucun téléchargement"
  exit 0
fi

# ---------------------------------------------------------------------------
# 2. GET vers le fichier de sortie (stream, pas de body temp)
# ---------------------------------------------------------------------------
GET_OPTS=""
if [ "$RESUME" -eq 1 ] && [ -f "$OUT" ]; then
  GET_OPTS="-C -"
  log "Reprise depuis $(stat -f%z "$OUT" 2>/dev/null || stat -c%s "$OUT") octets"
elif [ -f "$OUT" ]; then
  log "ATTENTION: $OUT existe, il sera écrasé (utilise --resume pour reprendre)"
fi

mkdir -p "$(dirname "$OUT")"

# shellcheck disable=SC2086
HTTP_CODE=$(curl $CURL_OPTS $GET_OPTS --progress-bar -o "$OUT" -D "$HDR_FILE" -w '%{http_code}' \
  --retry 3 --retry-delay 5 --retry-all-errors \
  "https://${HOST}/${BUCKET}/${ENC_KEY}" \
  -H "Authorization: Bearer $TOKEN") || { log "curl échec sur GET"; exit 3; }

case "$HTTP_CODE" in
  200|206) ;;
  *) log "GET HTTP $HTTP_CODE"; head -c 300 "$OUT" >&2; echo >&2; rm -f "$OUT"; exit 3 ;;
esac

# ---------------------------------------------------------------------------
# 3. Intégrité : taille, puis MD5 si l'ETag n'est pas multipart ("-N")
# ---------------------------------------------------------------------------
LOCAL_SIZE="$(stat -f%z "$OUT" 2>/dev/null || stat -c%s "$OUT")"
if [ -n "$SIZE" ] && [ "$LOCAL_SIZE" != "$SIZE" ]; then
  log "Intégrité KO: taille locale $LOCAL_SIZE ≠ attendue $SIZE"
  exit 4
fi

case "$ETAG" in
  ""|*-*) log "ETag multipart ou absent : contrôle par taille uniquement ($LOCAL_SIZE octets)" ;;
  *)
    if command -v md5sum >/dev/null; then LOCAL_MD5="$(md5sum "$OUT" | cut -d' ' -f1)"
    else LOCAL_MD5="$(md5 -q "$OUT")"; fi
    if [ "$LOCAL_MD5" != "$ETAG" ]; then
      log "Intégrité KO: MD5 $LOCAL_MD5 ≠ ETag $ETAG"
      exit 4
    fi
    log "MD5 vérifié"
    ;;
esac

log "OK → $OUT ($LOCAL_SIZE octets)"
exit 0
