output "restore_id" {
  value = restapi_object.restapi.id
}

# bonus : la réponse complète du POST si tu veux le status initial
output "restore_create_response" {
  value     = restapi_object.restapi.create_response
  sensitive = true
}
