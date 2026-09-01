IAM_TOKEN=$(curl -s -X POST "https://iam.cloud.ibm.com/identity/token" \
  -d "grant_type=urn:ibm:params:oauth:grant-type:apikey&apikey=${API_KEY}" \
  | grep -o '"access_token":"[^"]*' | cut -d'"' -f4)
