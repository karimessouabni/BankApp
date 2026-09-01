# ── Backup policy : nommage unique et recréation sûre ────────────────────────
# À recoller dans le main.tf racine du workspace bucket, en remplacement du
# bloc backup_policies au nom en dur "backup-policy".
#
# Contexte du bug corrigé : les backup policies COS ne sont pas modifiables,
# leur suppression n'est pas instantanée (recovery ranges), et un bucket
# n'accepte qu'une seule policy. Recréer une policy sous le même nom pendant
# que l'ancienne se supprime fait retomber sur l'ancienne — et sur son ancien
# initial_delete_after_days.
#
# Ce fichier règle la moitié Terraform (nom unique par (re)création). L'autre
# moitié vit dans le DAG : entre la suppression et la recréation, attendre la
# disparition effective de la policy (GET des policies du bucket jusqu'à liste
# vide, avec timeout), comme pour wait_for_scheduled_time.

resource "random_id" "backup_policy" {
  count       = var.backup_enabled ? 1 : 0
  byte_length = 3

  # Le suffixe est stable tant que ces valeurs ne changent pas ; changer la
  # durée ou le vault produit un nouveau nom, donc une nouvelle policy —
  # cohérent avec leur immuabilité côté COS, et plus aucune collision avec
  # une ancienne policy en cours de suppression.
  keepers = {
    bucket = module.naming_bucket.name
    days   = var.initial_delete_after_days
    vault  = var.target_backup_vault_crn
  }
}

locals {
  backup_policies = !var.backup_enabled ? [] : [
    {
      policy_name               = "${module.naming_bucket.name}-bp-${random_id.backup_policy[0].hex}"
      target_backup_vault_crn   = var.target_backup_vault_crn
      initial_delete_after_days = var.initial_delete_after_days
    }
  ]
}

# Dans le bloc module "bucket" du main.tf racine :
#   backup_policies = local.backup_policies
#
# Provider requis (versions.tf) :
#   random = { source = "hashicorp/random" }
#
# Si le nom risque de dépasser la limite du champ policy_name, tronquer :
#   substr("${module.naming_bucket.name}-bp-${random_id.backup_policy[0].hex}", 0, N)
