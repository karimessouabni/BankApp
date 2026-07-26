state_manager.push_state({
    "recovery_ranges": [
        {
            "recovery_range_id": r["recovery_range_id"],
            "range_start_time": r["range_start_time"],
            "range_end_time": r["range_end_time"],
        }
        for r in ranges[:20]          # les N plus récents, pas tout l'historique
    ],
    "recovery_ranges_fetched_at": datetime.now(timezone.utc).isoformat(),
})
