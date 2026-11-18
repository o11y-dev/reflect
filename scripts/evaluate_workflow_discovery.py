"""Offline procedure fidelity evaluation. Private captures never leave the machine."""
from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path


def evaluate_cases(cases: list[dict], candidates: list[dict]) -> dict:
    indexed = {case['case_id']:case for case in cases}
    splits = defaultdict(set)
    for case in cases:
        splits[case['session_group']].add(case['split'])
    report = dict.fromkeys(('unsupported_steps','contaminated_targets','false_outcomes',
                           'unexpected_proposals','unknown_references','duplicate_suggestions'), 0)
    report['split_leaks'] = sum(len(values)>1 for values in splits.values())
    report['duplicate_suggestions'] = sum(n-1 for n in Counter(c['group_key'] for c in candidates).values())
    covered, unexpected, failures = set(), set(), []
    for candidate in candidates:
        for ref in candidate.get('case_ids', []):
            case = indexed.get(ref)
            if case is None:
                report['unknown_references'] += 1
                failures.append({'case_id':ref,'reason':'unknown_reference'})
                continue
            covered.add(ref)
            if case['expected_disposition'] == 'abstain':
                unexpected.add(ref)
            for field, failed in (
                ('unsupported_steps', len(set(candidate.get('observed_steps',[]))-set(case['expected_steps']))),
                ('contaminated_targets', int(candidate.get('target') != case['expected_target'])),
                ('false_outcomes', int(candidate.get('outcome','unknown') not in ('unknown', case['expected_outcome']))),
            ):
                report[field] += failed
                if failed:
                    failures.append({'case_id':ref,'reason':field})
    report['unexpected_proposals'] = len(unexpected)
    eligible = {case['case_id'] for case in cases if case['expected_disposition']=='propose'}
    report.update(eligible_cases=len(eligible), covered_eligible_cases=len(eligible & covered),
                  missed_eligible_cases=sorted(eligible-covered), abstained_cases=sorted(set(indexed)-covered),
                  failures=failures, safe=not any(report.values()), human_review='pending', promotion_approved=False)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('cases',type=Path)
    parser.add_argument('candidates',type=Path)
    args = parser.parse_args()
    print(json.dumps(evaluate_cases(json.loads(args.cases.read_text()),json.loads(args.candidates.read_text())),indent=2))


if __name__ == '__main__':
    main()
