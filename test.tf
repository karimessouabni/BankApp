        # 4) Terraform : workspace éphémère
        logger.info("Terraform create workspace")

        ws_id_created = None  # utilisé par le finally

        # --- purge d'un éventuel workspace résiduel ---
        stale_ws = None
        try:
            stale_ws = tf.workspaces.get_by_name(ws_name)
        except Exception as ex:
            if getattr(ex, "code", None) == 404:
                stale_ws = None          # cas nominal : rien à purger
            else:
                raise

        if stale_ws is not None:
            logger.info("stale workspace %s found, deleting", stale_ws.id)
            stale_ws.delete()
            for _ in range(60):          # ~5 min max
                try:
                    tf.workspaces.get_by_name(ws_name)
                    time.sleep(5)
                except Exception as ex:
                    if getattr(ex, "code", None) == 404:
                        logger.info("stale workspace fully deleted")
                        break
                    raise
            else:
                raise TimeoutError(f"workspace {ws_name} still present after delete")

        # --- create + plan + apply ---
        create_ws_result = create_or_update_ws(
            tf,
            ws_name,
            ENVIRONMENT,
            tf_directory,
            variables,
            description,
            secrets["gitlab_token"],
        )
        ws_id_created = create_ws_result["id"]
        state_manager.push_state({"workspace_name": ws_name, "workspace_id": ws_id_created})

        tf_workspace = tf.workspaces.get_by_id(ws_id_created)
        plan_activity = tf_workspace.plan()
        apply_activity = tf_workspace.apply()
        logger.info("apply_activity %s", apply_activity)

        # --- récupérer le restore_id via les outputs, puis persister ---
        outputs = tf_workspace.outputs()          # adapte au nom exact dans la lib
        restore_id = outputs["restore_id"]
        update_restore_status_and_id(
            session,
            id=db_restore.id,
            restore_id=restore_id,
            status=RestoreStatus.IN_PROGRESS,
        )
        return ws_id_created
