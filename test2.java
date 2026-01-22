Oui — je te fais une version 100% compatible avec ta structure actuelle (computePauseMs, waitIfPaused, activatePause, addEqualJitter) ✅
Et au passage je corrige un bug important dans ton code actuel :

⚠️ Bug actuel

Dans ton activatePause() tu fais :

pauseUnitMs = Math.max(pauseUnitMs, System.currentTimeMillis() + ms);

Donc pauseUnitMs devient un timestamp (pauseUntil).

Mais dans waitIfPaused() tu fais :

Thread.sleep(pauseUnitMs);

Là tu utilises ce timestamp comme une durée → ça peut dormir énormément / n’importe comment.

✅ La solution : pauseUnitMs doit être traité comme pauseUntilMs (un “jusqu’à quand”), et waitIfPaused() doit dormir le temps restant.

⸻

✅ Version corrigée + jitter (compatible avec ton code)

Tu peux garder le nom pauseUnitMs si tu veux, mais je te conseille pauseUntilMs pour éviter les erreurs.

// Si tu peux, renomme : pauseUnitMs -> pauseUntilMs
private volatile long pauseUntilMs = 0L;

private long computePauseMs(Exception e) {
    if (e instanceof org.springframework.web.client.HttpClientErrorException.TooManyRequests) {
        return 60_000;
    }
    if (e instanceof org.springframework.web.client.HttpServerErrorException
            || e instanceof org.springframework.web.client.ResourceAccessException) {
        return 15_000;
    }
    if (e instanceof CustomGenericException) {
        return 60_000;
    }
    return 10_000;
}

private void waitIfPaused() {
    long now = System.currentTimeMillis();
    long remaining = pauseUntilMs - now;

    if (remaining <= 0) {
        return; // pas de pause active
    }

    log.info("Batch paused for remainingMs={}", remaining);
    try {
        Thread.sleep(remaining);
    } catch (InterruptedException e) {
        Thread.currentThread().interrupt();
    }
}

private void activatePause(long baseMs) {
    if (baseMs <= 0) return;

    // ✅ jitter sur la durée (PAS sur currentTimeMillis)
    long jitteredMs = addEqualJitter(baseMs); // ou full jitter si tu veux

    long now = System.currentTimeMillis();
    long newUntil = now + jitteredMs;

    // ✅ On ne raccourcit jamais une pause déjà active
    pauseUntilMs = Math.max(pauseUntilMs, newUntil);

    log.info("activatePause => baseMs={}, jitteredMs={}, pauseUntilMs={}",
            baseMs, jitteredMs, pauseUntilMs);
}

private long addEqualJitter(long baseMs) {
    if (baseMs <= 0) return 0L;
    long half = baseMs / 2;
    // renvoie [half .. baseMs]
    return half + java.util.concurrent.ThreadLocalRandom.current().nextLong(half + 1);
}


⸻

Comment l’utiliser dans ton flow

À chaque boucle / avant appel API :

waitIfPaused();

Quand tu catches une exception :

long ms = computePauseMs(e);
activatePause(ms);


⸻

Option meilleure (si tu veux) : FULL JITTER (plus “standard”)

Si tu veux remplacer addEqualJitter par du full jitter (random 0..base) :

private long addFullJitter(long baseMs) {
    if (baseMs <= 0) return 0L;
    return java.util.concurrent.ThreadLocalRandom.current().nextLong(baseMs + 1);
}

Et dans activatePause :

long jitteredMs = addFullJitter(baseMs);


⸻

Si tu me montres où tu appelles waitIfPaused() (avant chaque bucket ? avant chaque requête ?), je te dis l’endroit exact le plus safe pour éviter de relancer des appels trop tôt.