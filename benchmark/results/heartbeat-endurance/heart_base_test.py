{
  "run": {
    "tool": "heartbeat_endurance",
    "timestamp": "2026-08-09T15:25:42"
  },
  "scenarios": [
    {
      "scenario": "heartbeat_enabled",
      "url": "https://bench-heartbeat.example.test/enabled-1786269330",
      "config": {
        "work_duration_s": 6.0,
        "lease_ttl_s": 2.0,
        "heartbeat_interval_s": 0.6666666666666666,
        "recovery_interval_s": 0.3
      },
      "work_elapsed_s": 6.002311706542969,
      "claim_lost_mid_work": false,
      "recovery_sweep_log": [],
      "reclaimed_at_any_point": false,
      "final_mark_visited_took_effect": true,
      "final_status_counts": {
        "queued": 0,
        "inflight": 0,
        "retry_scheduled": 0,
        "visited": 1,
        "skipped": 0,
        "failed_permanent": 0
      },
      "matches_expected_behavior": true
    },
    {
      "scenario": "heartbeat_disabled",
      "url": "https://bench-heartbeat.example.test/disabled-1786269336",
      "config": {
        "work_duration_s": 6.0,
        "lease_ttl_s": 2.0,
        "heartbeat_interval_s": 0.6666666666666666,
        "recovery_interval_s": 0.3
      },
      "work_elapsed_s": 6.000793695449829,
      "claim_lost_mid_work": false,
      "recovery_sweep_log": [
        {
          "t": 1786269338.2677402,
          "reclaimed": 1,
          "requeued": 0
        },
        {
          "t": 1786269338.5689049,
          "reclaimed": 0,
          "requeued": 1
        }
      ],
      "reclaimed_at_any_point": true,
      "final_mark_visited_took_effect": false,
      "final_status_counts": {
        "queued": 1,
        "inflight": 0,
        "retry_scheduled": 0,
        "visited": 0,
        "skipped": 0,
        "failed_permanent": 0
      },
      "matches_expected_behavior": true
    }
  ]
}