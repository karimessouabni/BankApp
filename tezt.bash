API_KEY="ton_api_key"
COS_ENDPOINT="https://s3.eu-de.cloud-object-storage.appdomain.cloud"
BUCKET="mon-bucket"

# 1. Échanger l'API key contre un token IAM
IAM_TOKEN=$(curl -s -X POST "https://iam.cloud.ibm.com/identity/token" \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "grant_type=urn:ibm:params:oauth:grant-type:apikey&apikey=${API_KEY}" \
  | jq -r .access_token)

# 2. List objects
curl -s "${COS_ENDPOINT}/${BUCKET}?list-type=2" \
  -H "Authorization: Bearer ${IAM_TOKEN}"
curl -s "https://s3.direct.eu-fr2.cloud-object-storage.appdomain.cloud/bu002i012826?list-type=2&prefix=0DD0/" \
  -H "Authorization: Bearer $TOKEN"



curl -s -X PUT "${COS_ENDPOINT}/${BUCKET}/mon-fichier.txt" \
  --aws-sigv4 "aws:amz:${REGION}:s3" \
  --user "${ACCESS_KEY}:${SECRET_KEY}" \
  -T ./mon-fichier.txt



ACCESS_KEY="18351fec6288463e912ca1ccd3d3c6d2"
SECRET_KEY="758f92717432374aa45ee8e17cc9760ddf19be7d05c21253"

curl -s -w '\nHTTP: %{http_code}\n' \
  "https://s3.direct.eu-fr2.cloud-object-storage.appdomain.cloud/bu002i012826?list-type=2&max-keys=5" \
  --aws-sigv4 "aws:amz:eu-fr2:s3" \
  --user "${ACCESS_KEY}:${SECRET_KEY}"



ACCESS_KEY="18351fec6288463e912ca1ccd3d3c6d2"
SECRET_KEY="758f92717432374aa45ee8e17cc9760ddf19be7d05c21253"
ENDPOINT="https://s3.direct.eu-fr2.cloud-object-storage.appdomain.cloud"
BUCKET="bu002i012826"

echo "test hmac $(date)" > /tmp/test-hmac.txt

curl -s -w '\nHTTP: %{http_code}\n' -X PUT \
  "${ENDPOINT}/${BUCKET}/test-hmac-$(date +%s).txt" \
  --aws-sigv4 "aws:amz:eu-fr2:s3" \
  --user "${ACCESS_KEY}:${SECRET_KEY}" \
  -T /tmp/test-hmac.txt



