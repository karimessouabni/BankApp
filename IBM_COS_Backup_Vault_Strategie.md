# IBM Cloud Object Storage — Backup Vault, Object Lock et stratégie de protection des buckets

> Objectif : expliquer le fonctionnement du Backup Vault COS, ses différences avec Object Lock + versioning, et donner des règles de choix pour nos buckets.

---

## 1. Vue d'ensemble

| Composant | Rôle |
|---|---|
| **Bucket source** | Bucket à protéger. Versioning **obligatoire** pour attacher une backup policy. |
| **BackupVault** | Ressource COS dédiée, opaque (pas de listing, pas de GET d'objets), qui stocke les backups sous forme de *recovery ranges*. Provisionnée dans une instance COS, mais gérée différemment d'un bucket. |
| **BackupPolicy** | Attachée à un bucket source, cible un vault. Type `continuous`. Déclenche une synchro initiale complète puis une synchro continue. |
| **RecoveryRange** | Période continue de couverture pour un bucket dans un vault (`range_start_time` → `range_end_time`). |
| **Restore** | Opération ponctuelle : écrit l'état du bucket à un instant T dans un bucket cible. |

Principe clé : **le backup est continu, le restore est ponctuel.** Une fois la policy attachée, rien à relancer. Un restore est une commande unique qui produit un état figé ; il ne se resynchronise pas ensuite.

---

## 2. Backup policy et recovery ranges

- Jusqu'à **3 policies par bucket**, chacune vers un vault **différent** (usage typique : vault local, vault autre région, vault autre compte). Une policy = une copie complète + un flux de synchro facturés.
- Chaque policy active alimente **un** range dans son vault. Un bucket qui ne subit ni détachement ni erreur de policy n'a qu'un seul range par vault.
- Détacher puis rattacher une policy crée un **nouveau range** (nouvelle synchro initiale complète) et laisse un trou de couverture. L'ancien range se fige et vit jusqu'à expiration de sa rétention.
- **RPO ≤ 1 h** : IBM s'engage à ce qu'un objet écrit à T soit dans le vault au plus tard à T+1h. `range_end_time` reflète ce qui est réellement restaurable, pas « maintenant ». L'état le plus récent garanti restaurable est donc T−1h.

> Terraform : protéger `ibm_cos_backup_policy` avec `lifecycle { prevent_destroy = true }` pour éviter de couper la continuité par un `apply` malheureux.

---

## 3. Rétention : ce que ça veut vraiment dire

La rétention (`delete_after_days`) est une **fenêtre glissante de points de restauration**, pas une durée de vie du backup.

- Le vault conserve tout ce qui est nécessaire pour reconstituer le bucket à *n'importe quel instant* des N derniers jours, et purge le reste en arrière-plan.
- Objet supprimé ou écrasé à J : restaurable jusqu'à J+N. À J+N, la version disparaît **physiquement** du vault. Le « delete marker » n'est pas une protection au-delà de la fenêtre.
- Les objets encore présents dans le source restent dans le vault tant qu'ils existent : le vault n'est jamais « vide » tant que le bucket ne l'est pas.
- Rétention **extensible, jamais raccourcie**. Le vault ne peut pas être supprimé tant qu'une policy est attachée ou qu'un range n'a pas entièrement expiré.
- Avec 1 jour de rétention et 1 h de RPO, la fenêtre utile est ~23 h. À proscrire : le délai de détection réaliste d'un incident dépasse presque toujours 24 h.

**Recommandation** : 7 j minimum, 30 j pour les buckets critiques.

---

## 4. Restore

### Comportement
- Seul `restore_type = in_place` existe (le champ est réservé pour d'autres types, non annoncés).
- **Additif, jamais destructif** : écrit les versions courantes et delete markers de l'instant T dans la cible, en conservant `versionId`, `LastModified`, `ETag`. Ne supprime rien de ce qui est déjà dans la cible.
- Granularité = **bucket entier**. Pas de restore par préfixe ou par objet : restaurer dans un bucket temporaire, puis copier ce qu'il faut.
- Les tags d'objets restaurés sont les derniers connus sur le source, pas ceux de l'instant T. ACL/policies bucket ne sont pas restaurées.
- Asynchrone : poller `GET /restores/{id}` jusqu'à `status: complete` ou `failed`. Max **3 restores en parallèle** par vault.

### Prérequis cible
- Bucket existant, **versionné**, même région que le vault, sans firewall legacy, **sans Object Lock**, sans backup policy attachée.
- S2S `vault → bucket cible` (rôle Writer/Manager, action `bucket.restore_sync`) — requise même dans la même instance.

### Appel API

```http
POST /backup_vaults/{vault}/restores
Host: config.cloud-object-storage.cloud.ibm.com
Authorization: Bearer {token}
Content-Type: application/json

{
  "recovery_range_id": "<uuid via GET /backup_vaults/{vault}/recovery_ranges>",
  "restore_point_in_time": "2026-09-14T22:00:00Z",
  "restore_type": "in_place",
  "target_resource_crn": "crn:v1:bluemix:public:cloud-object-storage:global:a/<account>:<instance>:bucket:<nom>"
}
```

Les quatre champs sont obligatoires. `restore_point_in_time` doit être compris entre `range_start_time` et `range_end_time` ; pas de valeur par défaut. Pour « le plus récent possible », utiliser `range_end_time` moins quelques minutes.

---

## 5. Autorisations service-à-service (S2S)

Une autorisation S2S se crée **dans le compte qui possède la ressource cible** et désigne le compte source.

| Flux | Source | Cible | Rôle |
|---|---|---|---|
| Backup | instance COS du bucket | instance COS du vault, `resourceType = backup-vault` | Backup Manager (ou Manager) |
| Restore | instance COS du vault | instance COS du bucket cible | Writer (ou Manager) |

- **Même instance** : les deux autorisations restent nécessaires (source = cible).
- **Instances différentes, même compte** : idem, instance IDs différents.
- **Comptes différents** : la S2S backup se crée dans le compte du vault avec `source_service_account = <compte du bucket>` ; la S2S restore se crée dans le compte du bucket cible avec `source_service_account = <compte du vault>`.

```hcl
# Côté compte du vault
resource "ibm_iam_authorization_policy" "bucket_to_vault" {
  source_service_name         = "cloud-object-storage"
  source_service_account      = var.bucket_account_id
  source_resource_instance_id = var.bucket_cos_instance_guid
  target_service_name         = "cloud-object-storage"
  target_resource_instance_id = var.vault_cos_instance_guid
  roles                       = ["Backup Manager"]
  resource_attributes { name = "resourceType"; operator = "stringEquals"; value = "backup-vault" }
  resource_attributes { name = "resource";     operator = "stringEquals"; value = var.vault_name }
}

# Côté compte du bucket cible
resource "ibm_iam_authorization_policy" "vault_to_bucket" {
  source_service_name         = "cloud-object-storage"
  source_service_account      = var.vault_account_id
  source_resource_instance_id = var.vault_cos_instance_guid
  target_service_name         = "cloud-object-storage"
  target_resource_instance_id = var.bucket_cos_instance_guid
  roles                       = ["Writer"]
}
```

Pièges : `depends_on` + `time_sleep` (~30 s) entre la S2S et la backup policy (propagation IAM, sinon 400) ; les policies applicatives doivent être scopées **bucket par bucket** si vault et buckets partagent une instance, sinon un `Writer` instance atteint le vault.

---

## 6. Où placer le vault

| Placement | Protège contre | Ne protège pas contre | Verdict |
|---|---|---|---|
| Même instance que le bucket | Erreur humaine, bug applicatif ; rétention non raccourcissable | Clé/rôle instance compromis, suppression d'instance, incident régional | Quasi redondant avec Object Lock |
| Même compte, instance + resource group dédiés (autre région possible) | + identités applicatives, Managers de l'instance prod, perte d'instance/région | Admin du compte / compte compromis | **Minimum sérieux** |
| Compte séparé | Tout ce qui précède + compte prod compromis | — | La raison d'être du produit ; à réserver aux buckets critiques / exigence cyber |

Compléments quel que soit le placement : access group « backup admins » distinct ; alerte Activity Tracker sur `backup-vault.delete`, `backup-policy.delete`, modification de rétention.

---

## 7. Object Lock + versioning vs Backup Vault

| Critère | Object Lock (compliance) + lifecycle | Backup Vault |
|---|---|---|
| Où est la donnée | Dans le bucket lui-même | Copie hors du bucket (instance / région / compte) |
| Immutabilité | Par version, même pour le propriétaire du compte | Rétention non raccourcissable, protégée par IAM uniquement |
| RPO | 0 | ≤ 1 h |
| Récupération | Immédiate, `GET ?versionId=` | Restore à lancer, durée ∝ volume |
| Point-in-time bucket entier | Manuel (filtre `LastModified ≤ T` clé par clé) | Natif |
| Suppression bucket / instance / compte | Non couvert | Couvert (si vault placé ailleurs) |
| Réversibilité | Aucune en mode compliance | Policy détachable, expiration propre |
| Volume stocké (rétention R) | Versions produites pendant R | Copie de base **+** versions pendant R |
| Coût | Classe du bucket ; moins cher ou égal à volume identique | Tarif vault (~Standard, à vérifier par région) + opérations de synchro + egress cross-région |

**Cas où le vault est réellement avantageux :**
1. Exigence de copie hors du périmètre de faute (résilience cyber, audit, DORA).
2. Restauration point-in-time d'un gros bucket après corruption progressive.
3. Rétention longue sur un bucket très chaud (garder le source léger).
4. Séparation des rôles : rétention hors de portée de l'équipe applicative, même admin.
5. Environnements où Object Lock compliance est trop rigide (buckets éphémères).

En dehors de ces cas, Object Lock + lifecycle suffit et coûte moins.

---

## 8. Lifecycle obligatoire sur le source

Le versioning requis par la backup policy n'a **aucune rétention par défaut** : sans lifecycle, chaque version écrasée reste indéfiniment. Le vault se purge seul ; le bucket source, non.

```xml
<LifecycleConfiguration>
  <Rule>
    <ID>expire-noncurrent</ID>
    <Status>Enabled</Status>
    <Filter><Prefix></Prefix></Filter>
    <NoncurrentVersionExpiration><NoncurrentDays>1</NoncurrentDays></NoncurrentVersionExpiration>
    <Expiration><ExpiredObjectDeleteMarker>true</ExpiredObjectDeleteMarker></Expiration>
  </Rule>
</LifecycleConfiguration>
```

- `NoncurrentDays = 1` suffit : le vault a déjà capturé la version (RPO ≤ 1 h) et la purge côté source n'affecte pas le vault.
- Avec Object Lock en plus, la lifecycle ne peut pas purger une version avant l'échéance du lock : la rétention lock devient le plancher de stockage du source.
- Même règle sur les buckets cibles de restore (chaque restore empile des versions), ou utiliser un bucket jetable par restore.

Ordre de grandeur, bucket 1 To réécrit chaque jour, rétention 30 j : source ≈ 2 To, vault ≈ 31 To. Le poids est dans le vault, ce qui est la bonne place.

---

## 9. Matrice de décision

| Besoin | Solution |
|---|---|
| Récupérer un objet supprimé/écrasé dans les X derniers jours | Object Lock (X jours) + lifecycle X jours |
| Idem + survivre à la perte du bucket/instance/région | + Vault dans instance & RG dédiés, autre région |
| Idem + survivre à un compte compromis / exigence cyber | + Vault dans un compte séparé |
| Bucket dev/test recréé par pipeline | Vault court (7 j) ou rien ; pas d'Object Lock compliance |
| Bucket froid peu modifié | Object Lock seul |

**Proposition standard** : Object Lock 7 j sur tous les buckets prod + lifecycle ; Backup Vault 30 j dans un compte séparé (ou à défaut instance/RG dédiés autre région) uniquement sur les buckets classés critiques.

---

## 10. Points à vérifier avant mise en production

- Tarif au Go du Backup Vault dans notre région (grille IBM Cloud, ligne Backup Vault) pour chiffrer contre Object Lock.
- Comportement exact d'une suppression d'instance COS contenant un vault avec ranges actifs (blocage vs reclamation).
- Faisabilité du cross-account côté gouvernance BNP.
- Quota de restores parallèles (3) vs nombre de buckets critiques à restaurer simultanément en cas de DR.

## Références
- IBM Cloud Docs — Backing up your buckets (`cloud-object-storage-bvm-overview`)
- IBM Cloud Docs — Configuring backups (`cloud-object-storage-bvm-configure`)
- SDK config — `ResourceConfigurationV1` (`createBackupPolicy`, `createRestore`, `listRecoveryRanges`)
- Terraform : `ibm_cos_backup_vault`, `ibm_cos_backup_policy`, `ibm_iam_authorization_policy`, module `terraform-ibm-modules/cos`
