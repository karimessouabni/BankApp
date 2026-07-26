Deux endroits — et il faut corriger ton `finally` actuel qui est cassé :

**1. Le delete AVANT le create** — dans `create_tf_workspace_and_launch_restore`, juste avant l'appel à `create_or_update_ws` (vers la ligne ~300, avant `state_manager.push_state`) :

```python
    # --- purge d'un éventuel workspace résiduel ---
    ws_id_created = None          # ← initialisé ici, utilisé par le finally
    try:
        for ws in tf.workspaces.list():        # adapte au nom exact de la lib
            if ws.name == ws_name:
                logger.info("stale workspace %s found, deleting", ws.id)
                tf.workspaces.get_by_id(workspace_id=ws.id).delete()
                # poll jusqu'à disparition
                for _ in range(60):
                    try:
                        tf.workspaces.get_by_id(workspace_id=ws.id)
                        time.sleep(5)
                    except Exception as ex:
                        if getattr(ex, "code", None) == 404:
                            break
                        raise
                break
    except Exception:
        logger.warning("stale ws lookup/delete failed", exc_info=True)
```

**2. Ton `finally` lignes 347-351 référence `workspace['workspace_id']` qui n'existe pas** (la ligne qui le définissait est commentée dans le `else`) → il lève à chaque fois, l'except l'avale, et **le cleanup ne s'exécute jamais**. C'est très probablement pour ça que ton vieux workspace survit. Remplace par :

```python
    finally:
        if ws_id_created:
            try:
                tf.workspaces.get_by_id(workspace_id=ws_id_created).delete()
            except Exception:
                logger.warning("ws cleanup failed", exc_info=True)
```

et dans le corps du `try`, après le create, alimente la variable :

```python
    ws_id_created = create_ws_result["id"]
```

**3. Supprime la branche `else` lignes 335-338** — elle retourne un `workspace['workspace_id']` inexistant (`NameError` garanti) et son concept même ("workspace already created" → on réutilise) est ce qu'on veut éliminer. Avec le delete-avant du point 1, le create s'exécute toujours : plus besoin de if/else.

Ordre final dans la fonction : purge → create (`ws_id_created = ...`) → push_state → plan + wait → apply + wait → outputs → update DB → `finally` cleanup. Relance ensuite et vérifie dans les logs Airflow que tu vois `stale workspace ... deleting` au premier run (il va nettoyer le workspace pourri actuel) — c'est ton signal que le chemin s'exécute enfin.