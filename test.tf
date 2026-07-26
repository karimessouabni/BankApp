output "restore_id" {
  value = restapi_object.restapi.id
}

# bonus : la réponse complète du POST si tu veux le status initial
output "restore_create_response" {
  value     = restapi_object.restapi.create_response
  sensitive = true
}

r = requests.get(f"{BASE}/v1/workspaces/{ws_id}/output", headers=HDRS)
r.raise_for_status()
# structure : [{"output_values": [{"restore_id": {"value": "..."}}]}]
restore_id = r.json()[0]["output_values"][0]["restore_id"]["value"]

