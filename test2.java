Parfait 👍
Voici la version clean et adaptée à ton projet avec Lombok + Java 8, prête à coller.

Je te donne :
	1.	SQL
	2.	Entity BatchRun (Lombok, Java 8)
	3.	Repository
	4.	Bonnes pratiques d’utilisation dans ton batch

⸻

1️⃣ SQL – table batch_run (PostgreSQL)

CREATE TABLE IF NOT EXISTS batch_run (
  id              VARCHAR(36) PRIMARY KEY,
  type            VARCHAR(50) NOT NULL,
  status          VARCHAR(20) NOT NULL, -- QUEUED / RUNNING / DONE / FAILED / STOPPED

  created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMP NOT NULL DEFAULT NOW(),

  total_count     BIGINT NOT NULL DEFAULT 0,
  success_count   BIGINT NOT NULL DEFAULT 0,
  failed_count    BIGINT NOT NULL DEFAULT 0,
  skipped_count   BIGINT NOT NULL DEFAULT 0,

  last_error      TEXT
);

CREATE INDEX IF NOT EXISTS idx_batch_run_status ON batch_run(status);


⸻

2️⃣ Entity JPA BatchRun (Lombok + Java 8)

import lombok.*;
import javax.persistence.*;
import java.time.Instant;

@Entity
@Table(name = "batch_run")
@Data
@Builder
@NoArgsConstructor
@AllArgsConstructor
public class BatchRun {

  @Id
  @Column(length = 36, nullable = false)
  private String id;

  @Column(nullable = false, length = 50)
  private String type;

  @Column(nullable = false, length = 20)
  private String status;

  @Column(name = "created_at", nullable = false)
  private Instant createdAt;

  @Column(name = "updated_at", nullable = false)
  private Instant updatedAt;

  @Column(name = "total_count", nullable = false)
  private long totalCount;

  @Column(name = "success_count", nullable = false)
  private long successCount;

  @Column(name = "failed_count", nullable = false)
  private long failedCount;

  @Column(name = "skipped_count", nullable = false)
  private long skippedCount;

  @Lob
  @Column(name = "last_error")
  private String lastError;

  // Factory helper
  public static BatchRun newRun(String id, String type) {
    Instant now = Instant.now();
    return BatchRun.builder()
        .id(id)
        .type(type)
        .status("QUEUED")
        .createdAt(now)
        .updatedAt(now)
        .build();
  }

  public void markRunning() {
    this.status = "RUNNING";
    this.updatedAt = Instant.now();
  }

  public void markDone() {
    this.status = "DONE";
    this.updatedAt = Instant.now();
  }

  public void markFailed(String error) {
    this.status = "FAILED";
    this.lastError = error;
    this.updatedAt = Instant.now();
  }

  public void incSuccess() {
    this.successCount++;
    this.updatedAt = Instant.now();
  }

  public void incFailed(String error) {
    this.failedCount++;
    this.lastError = error;
    this.updatedAt = Instant.now();
  }

  public void incSkipped() {
    this.skippedCount++;
    this.updatedAt = Instant.now();
  }
}

✔ Compatible Java 8
✔ Lombok propre
✔ Méthodes utilitaires pour ton batch
✔ Pas de boilerplate inutile

⸻

3️⃣ Repository BatchRunRepository

import org.springframework.data.jpa.repository.JpaRepository;

public interface BatchRunRepository extends JpaRepository<BatchRun, String> {
}


⸻

4️⃣ Comment l’utiliser dans ton batch async (exemple réel)

Création du run

String runId = UUID.randomUUID().toString();

BatchRun run = BatchRun.newRun(runId, "WHITELIST_FORCE_UPDATE");
batchRunRepository.save(run);


⸻

Démarrage

run.markRunning();
batchRunRepository.save(run);


⸻

Pendant la boucle

try {
  updater.updateWhitelistForOneBucket(bucket, runId);
  run.incSuccess();
} catch (Exception e) {
  run.incFailed(e.getMessage());
}
batchRunRepository.save(run);


⸻

Fin

run.markDone();
batchRunRepository.save(run);


⸻

5️⃣ Bonnes pratiques (IMPORTANT)

✅ Ne mets PAS les compteurs uniquement en mémoire

Toujours persister dans BatchRun, sinon tu perds tout si crash.

✅ Sauvegarde le run régulièrement

Tous les X buckets (ex: 10) ou à chaque bucket si tu veux être ultra safe.

✅ Un run = un UUID

Ne réutilise jamais un runId.

⸻

🟢 Conclusion

Tu as maintenant :
	•	un batch async
	•	un suivi fiable en base
	•	une relance possible
	•	zéro retraitement inutile
	•	Lombok + Java 8 clean

Si tu veux, je peux maintenant :
	•	te donner le SQL pour ajouter les champs batchRunId / batchRunStatus sur Bucket
	•	te montrer le endpoint REST pour suivre le run
	•	ou t’aider à implémenter le retry FAILED only

Dis-moi 👍