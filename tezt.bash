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



