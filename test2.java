Oui — si tu t’en fiches des stats et du “run tracking”, tu peux **supprimer complètement `batch_run`** et faire **uniquement des champs dans `bucket`**. Et oui, à chaque relance tu récupères **les non exécutés** ou **FAILED**.

C’est même la solution **la plus simple**.

---

## Ce que tu dois avoir dans `bucket` (minimum viable)

### Minimum

* `batch_status` : `PENDING | INPROGRESS | DONE | FAILED`
* `batch_attempts` (optionnel mais utile)

### Recommandé (pour debug)

* `batch_last_error` (text)
* `batch_updated_at`

👉 Tu n’as pas besoin de `batch_run_id` si tu ne veux pas gérer des runs distincts.

---

## SQL (Postgres) — uniquement bucket

```sql
alter table bucket add column if not exists batch_status varchar(20);
alter table bucket add column if not exists batch_attempts int not null default 0;
alter table bucket add column if not exists batch_updated_at timestamp;
alter table bucket add column if not exists batch_last_error text;

create index if not exists idx_bucket_batch_status on bucket(batch_status);
```

---

## Règle de reprise (relance)

À chaque lancement :

* Tu prends : `batch_status is null OR batch_status in ('PENDING','FAILED')`
* Tu “claim” : tu passes `INPROGRESS` atomiquement
* Tu exécutes
* Tu marques `DONE` ou `FAILED`

✅ Résultat : si tu relances, **DONE ne sera jamais retraité**.

---

## Claim atomique (le point clé)

### Repository JPA

```java
public interface BucketRepository extends JpaRepository<Bucket, Long> {

  @Query("""
    select b.id from Bucket b
    where (b.batchStatus is null or b.batchStatus in (com.yourpkg.BatchStatus.PENDING, com.yourpkg.BatchStatus.FAILED))
      and b.status = :requiredStatus
      and b.action <> :deleteAction
    order by b.id
  """)
  List<Long> findIdsToProcess(@Param("requiredStatus") String requiredStatus,
                              @Param("deleteAction") String deleteAction,
                              Pageable pageable);

  @Modifying
  @Query("""
    update Bucket b
    set b.batchStatus = com.yourpkg.BatchStatus.INPROGRESS,
        b.batchAttempts = b.batchAttempts + 1,
        b.batchUpdatedAt = CURRENT_TIMESTAMP
    where b.id = :id
      and (b.batchStatus is null or b.batchStatus in (com.yourpkg.BatchStatus.PENDING, com.yourpkg.BatchStatus.FAILED))
  """)
  int claim(@Param("id") Long id);

  @Modifying
  @Query("""
    update Bucket b
    set b.batchStatus = com.yourpkg.BatchStatus.DONE,
        b.batchUpdatedAt = CURRENT_TIMESTAMP,
        b.batchLastError = null
    where b.id = :id
  """)
  int markDone(@Param("id") Long id);

  @Modifying
  @Query("""
    update Bucket b
    set b.batchStatus = com.yourpkg.BatchStatus.FAILED,
        b.batchUpdatedAt = CURRENT_TIMESTAMP,
        b.batchLastError = :err
    where b.id = :id
  """)
  int markFailed(@Param("id") Long id, @Param("err") String err);
}
```

> Remplace `com.yourpkg` par ton package.

---

## Boucle batch async (sans batch_run)

```java
@Async("batchExecutor")
@Transactional
public void runAsync() {

  int pageSize = 100;
  while (true) {

    List<Long> ids = bucketRepository.findIdsToProcess(
        Values.STATUS_SUCCESS,
        ValuesLib.ACTION_DELETE,
        PageRequest.of(0, pageSize)
    );

    if (ids.isEmpty()) break;

    for (Long id : ids) {

      if (bucketRepository.claim(id) != 1) continue;

      try {
        waitIfPaused();

        Bucket bucket = bucketRepository.findById(id).orElseThrow();
        updater.updateWhitelistForOneBucket(bucket, null); // pas de runId

        bucketRepository.markDone(id);

      } catch (Exception e) {
        long pauseMs = computePauseMs(e);
        activatePause(pauseMs);

        bucketRepository.markFailed(id, truncate(e.getMessage(), 2000));
      }
    }
  }
}
```

✅ Redémarrage : il reprend naturellement là où il s’est arrêté.

---

## Important : éviter les “INPROGRESS bloqués” si crash

Si ton process tombe au milieu, certains buckets resteront `INPROGRESS`.

Ajoute une règle de récupération :

* si `batch_status = INPROGRESS` **et** `batch_updated_at < now - 30min`
  → considérer comme FAILED ou PENDING.

Tu peux faire une requête de reset au début :

```sql
update bucket
set batch_status = 'FAILED'
where batch_status = 'INPROGRESS'
  and batch_updated_at < now() - interval '30 minutes';
```

Tu peux aussi le faire en JPA `@Modifying`.

---

## Conclusion claire

✅ Oui, tu peux faire **sans `batch_run`**
✅ Oui, tu relances et tu traites uniquement :

* `NULL / PENDING / FAILED`
  ✅ Avec `claim` + `DONE`, tu ne retraiteras pas les mêmes.

---

Si tu me confirmes juste le type de `bucket.id` (Long ou String) et le nom exact de tes colonnes JPA (`status`, `action`), je te renvoie le code final parfaitement aligné à ton projet (sans `com.yourpkg`).
