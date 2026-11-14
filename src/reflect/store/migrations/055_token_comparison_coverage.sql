-- Older context comparisons did not distinguish missing usage from zero.
-- Preserve their original evidence for audit, but withdraw unverified claims.
UPDATE measurements
SET cohort_json = json_remove(json_set(
      cohort_json,
      '$.superseded_token_comparison', json_object(
        'before_value', before_value, 'after_value', after_value,
        'before_count', before_count, 'after_count', after_count,
        'verdict', verdict, 'snapshots', json_extract(cohort_json, '$.execution_unit_snapshots')),
      '$.measurement_state', 'not_measurable',
      '$.measurement_reasons', json_array('Historical token coverage was not validated. Remeasure these frozen tasks with observed usage before drawing a conclusion.'),
      '$.eligibility.before.metric_count', NULL,
      '$.eligibility.after.metric_count', NULL,
      '$.eligibility.before.missing_metric_count', NULL,
      '$.eligibility.after.missing_metric_count', NULL
    ), '$.fingerprint', '$.execution_unit_snapshots'),
    before_value = NULL, after_value = NULL, delta = NULL,
    verdict = 'insufficient_data', confidence = 0
WHERE metric_name = 'context_outlier_sessions';
