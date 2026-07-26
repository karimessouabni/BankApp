ws_outputs = tf_workspace.outputs()   # [SchematicsWorkspaceOutputs(...)]

restore_id = None
for out in ws_outputs:                          # un par template_data
    for values in out.output_values:            # liste de dicts d'outputs
        if "restore_id" in values:
            restore_id = values["restore_id"]["value"]
            break
    if restore_id:
        break

if restore_id is None:
    raise ValueError("restore_id absent des outputs du workspace")

logger.info("restore_id=%s", restore_id)
