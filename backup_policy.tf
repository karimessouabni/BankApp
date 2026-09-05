# ── Backup policy : nommage unique, compatible avec le for_each du module ────
#
# Pourquoi pas un nom en dur : les policies COS sont immuables, leur
# suppression n'est pas instantanée, et un bucket n'accepte qu'une policy.
# Recréer sous le même nom pendant que l'ancienne se supprime fait retomber
# sur l'ancienne (et son ancien initial_delete_after_days).
#
# Pourquoi pas random_id directement : le module cle son for_each sur
# policy_name (main.tf:456) et les CLÉS d'un for_each doivent être connues au
# plan ; random_id.hex n'est créé qu'à l'apply → "Invalid for_each argument".
#
# D'où les deux fixes ci-dessous. Le 1 est recommandé : le module utilisé est
# la copie locale du projet (../modules/terraform-module-cos), la ligne est
# modifiable. Dans les deux cas, le DAG doit attendre la suppression effective
# de l'ancienne policy (GET des policies jusqu'à liste vide) avant de recréer.

# ── Fix 1 (recommandé) : clé statique dans le module, nom aléatoire conservé ─
# Dans ../modules/terraform-module-cos/main.tf ligne 456, remplacer la clé par
# l'index de la liste, connu au plan :
#
#   for_each = var.create_cos_bucket ? { for i, policy in var.backup_policies : i => policy } : {}
#
# (Avec une seule policy par bucket, l'index 0 est stable.) Le random_id
# ci-dessous fonctionne alors tel quel : nouveau nom à chaque changement de
# durée ou de vault, aucune collision avec une policy en cours de suppression.

resource "random_id" "backup_policy" {
  count       = var.backup_enabled ? 1 : 0
  byte_length = 3

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

# ── Fix 2 (sans toucher au module) : nom déterministe, connu au plan ────────
# Le nom encode la durée : changer 3 -> 9 change le nom, donc destroy + create
# de la policy. Limite : une recréation À DURÉE ÉGALE après une suppression
# manuelle reprend le même nom — l'attente de suppression côté DAG devient
# alors le seul rempart contre la collision.
#
# locals {
#   backup_policies = !var.backup_enabled ? [] : [
#     {
#       policy_name               = "${module.naming_bucket.name}-bp-${var.initial_delete_after_days}d"
#       target_backup_vault_crn   = var.target_backup_vault_crn
#       initial_delete_after_days = var.initial_delete_after_days
#     }
#   ]
# }

# Branchement dans le bloc module "bucket" du main.tf racine :
#   backup_policies = local.backup_policies
# Provider requis (versions.tf) : random = { source = "hashicorp/random" }
