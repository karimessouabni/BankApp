# IBM Cloud Object Storage — Backup Vault primer

Core idea, key concepts, architecture and IAM. Source: IBM Cloud docs (*Backing up your buckets*, *Restore*), Sept. 2026.

## 1. The idea in one sentence

COS Backup is a **policy-driven, continuous backup** service: you attach a **BackupPolicy** to a bucket, COS keeps syncing that bucket's data into an **immutable BackupVault**, and you can later **restore the bucket to any point in time** covered by the vault.

- **Not snapshots.** Traditional backups take periodic snapshots (daily/weekly), so anything changed in between is lost. Here, changes are streamed to the vault as they happen.
- **Any point in time.** You do not pick from a list of snapshots; you pick a timestamp inside a covered time window (the RecoveryRange).
- **RPO ≤ 1 hour.** IBM targets up to one hour for an object to be synced to the vault.
- **Resilient by design.** The vault is a separate resource, its data cannot be modified, and it can live in another region or even another IBM Cloud account — which protects you against ransomware, accidental deletion or a compromised production account.

## 2. Key concepts

| Term | What it is |
|---|---|
| `BackupVault` | A new COS resource (not a bucket) that stores backup data. Provisioned inside a COS service instance, alongside buckets, but managed with its own API and its own IAM roles. Data inside is unmodifiable. Can be encrypted with Key Protect. |
| Source bucket | The bucket you want protected. It carries the BackupPolicy. |
| `BackupPolicy` | Set on the source bucket. Points to a target BackupVault and defines the initial retention. As soon as it is active, COS syncs all existing data, then keeps syncing new writes for as long as the policy exists and is active. |
| `RecoveryRange` | The unit of backup data inside a vault: a continuous window of time [start, end] for one source bucket, inside which you can restore to any instant. One RecoveryRange is created per policy activation. |
| Retention | A `DeleteAfterDays` value on the RecoveryRange (initially from the policy, editable later on the vault). Controls how far back the range's start time may reach; older data is deleted in the background. |
| Restore | A user-triggered operation that writes the objects that were current at a chosen point in time into a target bucket. |
| Target bucket | The bucket that receives the restored objects. Must have versioning enabled and no legacy firewall rules. |

## 3. Architecture: how the pieces fit together

```
Source bucket ──(BackupPolicy: continuous sync)──▶ BackupVault [RecoveryRange: start ◀─▶ end]
BackupVault   ──(Restore: range id + point-in-time)──▶ Target bucket (versioned)
```

1. **Provision a BackupVault.** Same instance as the source bucket, a different instance, or an instance in a separate account/region (recommended for isolation).
2. **Grant the backup permission.** The source bucket's service instance needs `cloud-object-storage.backup-vault.sync` on the vault's service instance (service-to-service IAM authorization; included in Manager, Backup Manager and Backup Reader roles). If revoked, the policy reports an error.
3. **Set the BackupPolicy on the source bucket.** COS performs an initial full sync (progress reported in %); the RecoveryRange becomes visible only once initialization is complete. From then on the range's **end time moves forward** with every new write.
4. **Retention runs in the background.** The range's **start time moves forward** as data older than DeleteAfterDays is expired. A RecoveryRange only disappears completely when the policy is deleted AND retention has expired all its data (start == end).
5. **Restore when needed.** The vault's service instance needs `cloud-object-storage.bucket.restore_sync` on the target bucket's instance (Manager or Writer role) — required even within the same account/instance. You specify `recovery_range_id`, a `restore_point_in_time` inside [start, end], the `target_resource_crn` and `restore_type` (currently only `in_place`). Only the versions that were **current** at that instant are restored — never noncurrent versions. Max 3 concurrent restores per vault.

## 4. IAM: what to set up on an account that wants to use it

### 4.1 Service-to-service authorizations (mandatory, even inside one instance)

| Flow | Source | Target | Role | Key action |
|---|---|---|---|---|
| Backup | COS instance of the **source bucket** | COS instance of the **vault** | **Backup Manager** (Manager or Backup Reader also work) | `cloud-object-storage.backup-vault.sync` |
| Restore | COS instance of the **vault** | COS instance of the **target bucket** | **Writer** (or Manager) | `cloud-object-storage.bucket.restore_sync` |

Cross-account: the authorization is created **in the account that owns the target resource** (vault's account for backup, target bucket's account for restore), referencing the other account's instance by CRN.

### 4.2 Roles for users / access groups

| Profile | COS service role | Scope | Allows |
|---|---|---|---|
| Backup admin | **Backup Manager** | COS instance hosting the vault (**instance level, not bucket**) | Create/delete vaults, change retention, trigger and monitor restores, list recovery ranges |
| Ops / audit | **Backup Reader** | Vault's instance | List vaults, recovery ranges, statuses — read-only |
| Source bucket owner | **Manager** | Source bucket (can be bucket-scoped) | Set/remove the BackupPolicy and see its status |
| Target bucket owner | **Writer** or **Manager** | Target bucket | Bucket must be versioned and without legacy firewall; the restore itself is triggered vault-side |
| Everyone | **Viewer** (platform role) | COS instance | See the instance in the resource list / console |

### 4.3 Gotchas

- Listing vaults in the console uses an **instance-level** action (`account.list_account_backup_vaults`): an access group with only bucket-scoped policies gets a 403 on the Backup vaults tab even with Manager on its buckets. Give at least Backup Reader on the instance.
- Backup Reader/Manager are **separate** from bucket roles: Manager on a bucket grants nothing on the vault, and vice versa.
- Terraform/Schematics or Airflow service IDs need: Backup Manager on the vault instance + Manager on source buckets + Writer on target buckets.
- Strongest isolation: vault in a separate account where the production account holds **only** the backup service-to-service authorization and no human Backup Manager role.


## 7. Object Lock vs. Backup Vault: what the vault protects against that Object Lock does not

Object Lock protects **object versions inside the bucket**. Backup Vault protects **the state of the bucket, outside the bucket**. They answer different questions:

- Object Lock: *"nobody can delete this version here."*
- Backup Vault: *"even if here disappears, I can rebuild the bucket as it was at time T."*

| Risk | Object Lock | Backup Vault |
|---|---|---|
| Unlocked objects (written without retention, no bucket default) | Not protected — deletable like any object | Everything that transits through the bucket is captured |
| Retention period expires | Protection ends on day N+1 | Independent retention, extendable on the vault |
| Governance mode bypass (`bypass_governance_retention`) | A compromised identity with this right can shorten or remove locks | Vault is untouched; only Compliance mode is truly tamper-proof |
| COS instance deleted / account closed | Versions are locked, but the container is gone (reclamation, then permanent loss) | Cross-account vault survives |
| Encryption key (Key Protect / HPCS) deleted or destructively rotated | Objects exist but are unreadable | Vault has its own key |
| Regional outage | No replication — data unavailable, locked or not | Cross-region vault is a physical copy elsewhere |
| Mass overwrite / delete (ransomware, bad script) | Old versions exist, but must be found and re-promoted one by one | One operation restores the whole bucket at instant T — a real RTO |

**What the vault does not cover better:** data corrupted before being written is corrupted in both. With Compliance mode, long retention, same account and no regional constraint, the gain narrows to unlocked objects, container/key loss and consistent restore.

**Bottom line:** Object Lock is immutability, Backup Vault is a backup. A robust design uses both: versioning + Object Lock on the source, plus a cross-account, cross-region vault.



## 5. Things to remember

- A vault behaves like a **write-once ledger of a bucket's history**, not like a second bucket you can browse or edit.
- Restore is a **copy** into a bucket; it never touches the vault. Time to restore scales with the volume of data.
- Deleting a policy stops the sync but does not delete the backup data: retention decides when it goes.
- Cross-region + cross-account vault + Key Protect encryption = the strongest isolation posture.
## 6. Versioning is the foundation: source and target buckets

Backup Vault does not work on a bucket that is not versioned. Versioning is a hard prerequisite on **both sides** of the flow:

| Bucket | Why versioning is required |
|---|---|
| **Source bucket** | The `BackupPolicy` creation is rejected (HTTP 400) if the bucket does not have versioning enabled. The continuous sync relies on object versions to know exactly which object state existed at any instant — without version history, "restore to any point in time" is impossible. |
| **Target bucket** | A Restore is rejected if the target is not versioned. Restored objects are written as new versions, so the restore never destroys what is already in the bucket and can itself be rolled back. |

Also checked at policy creation: the source bucket must have fewer than 3 backup policies, and no two policies with the same name or the same target vault.

### How versioning is enabled

- **At bucket creation**: toggle *Object versioning* → Enabled in the console, or `object_versioning { enable = true }` in Terraform.
- **On an existing bucket**: `PUT Bucket?versioning` (S3 API) or `ibmcloud cos bucket-versioning-put --bucket <name> --versioning-configuration '{"Status":"Enabled"}'`. Existing objects become the "current" version; history starts from that moment.
- Versioning can only be **Enabled** or **Suspended** — it can never be fully disabled. To remove it, you must migrate the data to a new, non-versioned bucket.
- Check the state with `GET Bucket?versioning` — this is exactly what the backup service validates.






### The trap: retention policies (Immutable Object Storage) vs. Object Lock

IBM COS offers two WORM mechanisms, and only one of them is compatible with Backup Vault:

| Mechanism | Works with versioning? | Consequence for Backup Vault |
|---|---|---|
| **Retention policy** (Immutable Object Storage, "Add retention policy" on the bucket) | **No.** Enabling versioning on a bucket that has a retention policy fails, and adding a retention policy to a versioned bucket fails. Retention policies **cannot be removed** once set. | The bucket can **never** be a backup source or restore target. The only way out is to migrate the data to a new bucket. |
| **Object Lock** (Governance / Compliance modes, legal holds) | **Yes — it requires versioning.** Object Lock protects individual object *versions*, so versioning is mandatory and must be enabled first. | Fully compatible: a bucket can be immutable **and** backed up. |

### Why this matters for real protection

- Versioning alone protects against accidental overwrites and deletes inside the bucket, but not against a compromised account that deletes versions or the bucket itself.
- Backup Vault adds an **off-bucket, unmodifiable copy** with point-in-time restore — but it only exists if versioning is on.
- Object Lock on the source bucket adds **in-bucket immutability** (versions cannot be deleted before their retain-until date, even by admins in Compliance mode).
- The combination **versioning + Object Lock + Backup Vault (ideally cross-account)** covers the three failure modes: human error, malicious deletion, and loss of the production account.

**Rule of thumb when designing a new bucket:** enable versioning at creation, use Object Lock (not a retention policy) if immutability is required, and never add a retention policy to a bucket you may want to back up or restore into.

References: [Versioning objects](https://cloud.ibm.com/docs/cloud-object-storage?topic=cloud-object-storage-versioning) · [Object Lock](https://cloud.ibm.com/docs/cloud-object-storage?topic=cloud-object-storage-ol-overview) · [Restore prerequisites](https://cloud.ibm.com/docs/cloud-object-storage?topic=cloud-object-storage-restore)
**References:** [Backing up your buckets](https://cloud.ibm.com/docs/cloud-object-storage?topic=cloud-object-storage-bvm-overview) · [Restore](https://cloud.ibm.com/docs/cloud-object-storage?topic=cloud-object-storage-restore)
