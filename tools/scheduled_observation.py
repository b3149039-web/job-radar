"""Summarize 30 Beijing calendar days of downloaded public collection artifacts."""
import argparse
from collections import defaultdict
from datetime import date, datetime, timedelta
import json
from pathlib import Path

from collect_jobs import sources
from production_sync import SCHEDULED_SOURCES


def summarize(directory, last_day):
    first_day = last_day - timedelta(days=29)
    days = set()
    failed_days = set()
    backlog_days = set()
    open_ids = defaultdict(set)
    proposals = defaultdict(set)
    inserted = defaultdict(set)
    source_days = defaultdict(set)
    reviewed_days = set()
    written_days = set()
    for name in ('scheduled-collection.json', 'scheduled-preview.json', 'scheduled-write.json'):
        for path in directory.rglob(name):
            report = json.loads(path.read_text(encoding='utf-8'))
            moment = datetime.fromisoformat(report['observed_at'])
            if moment.tzinfo is None:
                raise ValueError('观察报告缺少时区。')
            day = moment.astimezone(sources.CST).date()
            if not first_day <= day <= last_day:
                continue
            days.add(day)
            outcome = report['outcome']
            if outcome == 'failed':
                failed_days.add(day)
                continue
            if outcome == 'preview':
                reviewed_days.add(day)
                if report.get('write_gate') == 'blocked_backlog':
                    backlog_days.add(day)
            if outcome == 'inserted':
                written_days.add(day)
            for source in SCHEDULED_SOURCES:
                counts = report['sources'].get(source)
                if counts is None:
                    continue
                source_days[source].add(day)
                open_ids[source].update(counts.get('open_ids', []))
                if outcome == 'preview':
                    proposals[source].update(counts.get('candidate_ids', []))
                if outcome == 'inserted':
                    inserted[source].update(counts.get('candidate_ids', []))
    result = {'from': first_day.isoformat(), 'through': last_day.isoformat(),
              'observed_days': len(days), 'read_only_review_days': len(reviewed_days),
              'write_blocked_by_backlog_days': len(backlog_days),
              'days_with_failure': len(failed_days), 'write_verified_days': len(written_days), 'sources': {}}
    for source in SCHEDULED_SOURCES:
        result['sources'][source] = {'open_posting_ids': len(open_ids[source]) if source_days[source] else None,
                                     'proposed_new_ids': len(proposals[source]) if reviewed_days else None,
                                     'inserted_ids': len(inserted[source]) if written_days else None}
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path, help='Directory with extracted scheduled artifacts')
    parser.add_argument('--through', type=date.fromisoformat, default=datetime.now(sources.CST).date())
    args = parser.parse_args()
    if not args.directory.is_dir():
        parser.error('观察报告目录不存在。')
    print(json.dumps(summarize(args.directory, args.through), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
