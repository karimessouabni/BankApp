@Async("batchExecutor")
@Transactional
public void runAsyncForceUpdateAll(String runId) {

  BatchRun run = batchRunRepository.findById(runId).orElse(null);
  if (run == null) {
    log.error("BatchRun not found for runId={}", runId);
    return;
  }

  run.markRunning();
  batchRunRepository.save(run);

  int pageSize = 100;
  int success = 0;
  int failed = 0;
  int skipped = 0;

  try {
    while (true) {

      // Toujours la "premiere page" des restants
      List<Long> ids = bucketRepository.findIdsToProcess(
          runId,
          Values.STATUS_SUCCESS,
          ValuesLib.ACTION_DELETE,
          PageRequest.of(0, pageSize)
      );

      if (ids.isEmpty()) break;

      for (Long id : ids) {

        // Claim atomique: si 0 -> deja pris/traite, on passe
        if (bucketRepository.claim(id, runId) != 1) {
          continue;
        }

        Bucket bucket = null;

        try {
          waitIfPaused();

          bucket = bucketRepository.findById(id)
              .orElseThrow(() -> new IllegalStateException("Bucket not found id=" + id));

          // (Optionnel) Si tu veux encore skipper ici, fais-le AVANT l’appel externe
          // mais normalement findIdsToProcess filtre deja status/action donc skipped ici devrait rester 0

          ValidateGenericAccess vga = apiServicesInterface.validateGenericAccess(
              bucket.getInstanceCos().getEcosystem(),
              null,
              Values.RIGHT_UPDATE,
              ValuesLib.RESOURCE_BUCKET,
              authorizationHeaderUtil.getAuthorizationHeader().get()
          );

          MetierN3 metierN3 = vga.getEcosystem().getMetierN3();

          bucket.bucketAllowedIps(
              bucketService.mapWhitelistIp(vga).stream()
                  .map(WhitelistIp::getAddress)
                  .collect(Collectors.toList())
                  .toString()
          );

          bucketService.forceUpdateBucket(bucket, metierN3);

          if (Objects.equals(bucket.getStatus(), Values.STATUS_FAILED)) {
            throw new CustomGenericException(HttpStatus.INTERNAL_SERVER_ERROR, "ERROR during force update");
          }

          bucket.setBatchRunStatus("DONE");
          bucketRepository.save(bucket);

          success++;
          run.incSuccess();

        } catch (Exception e) {
          long pauseMs = computePauseMs(e);
          activatePause(pauseMs);

          if (bucket == null) {
            bucket = bucketRepository.findById(id).orElse(null);
          }
          if (bucket != null) {
            bucket.setBatchRunStatus("FAILED");
            // optionnel: bucket.setBatchRunLastError(truncate(e.getMessage()));
            bucketRepository.save(bucket);
          }

          failed++;
          run.incFailed(e.getMessage());

          log.error("Bucket {} failed (pause {}ms)", id, pauseMs, e);

        } finally {
          // Tu peux optimiser en sauvegardant toutes les 10 iterations si tu veux
          batchRunRepository.save(run);
        }
      }
    }

    // statut final
    if (failed > 0) {
      run.setStatus("DONE_WITH_ERRORS");
    } else {
      run.markDone();
    }
    batchRunRepository.save(run);

    log.info("Run {} finished: success={} failed={} skipped={}", runId, success, failed, skipped);

  } catch (Exception fatal) {
    run.markFailed(fatal.getMessage());
    batchRunRepository.save(run);
    log.error("Run {} crashed", runId, fatal);
  }
}