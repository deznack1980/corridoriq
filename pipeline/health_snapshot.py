"""Read-only refresh supervision, suitable for Jarvis and scheduled reports.

python -m pipeline.health_snapshot --db PATH (never creates or migrates a DB).
"""
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def snapshot(path, now=None, stale_hours=36):
    now = now or datetime.now(timezone.utc)
    uri = Path(path).resolve().as_uri() + '?mode=ro'
    with sqlite3.connect(uri, uri=True) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA query_only=ON')
        rows = conn.execute('''SELECT j.slug,j.name,j.status,j.last_synced_at,
            j.last_sync_status, COUNT(p.id) AS permit_count,
            MAX(p.issued_date) AS newest_issued_date,
            MAX(p.filed_date) AS newest_filed_date
            FROM jurisdictions j LEFT JOIN permits p ON p.jurisdiction=j.slug
            GROUP BY j.slug ORDER BY j.slug''').fetchall()
    results = []
    for row in rows:
        item = dict(row)
        reasons = []
        if row['status'] != 'connected':
            reasons.append('source_pending')
        if row['last_sync_status'] not in ('success', 'success_with_errors'):
            reasons.append('refresh_unverified_or_failed')
        if row['last_sync_status'] == 'success_with_errors':
            reasons.append('partial_source_errors')
        try:
            stamp = datetime.fromisoformat(row['last_synced_at'].replace('Z', '+00:00'))
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            age = (now-stamp).total_seconds()/3600
            if age < 0 or age > stale_hours:
                reasons.append('invalid_or_stale_refresh')
        except (ValueError, AttributeError):
            reasons.append('missing_refresh_timestamp')
        dates = [row['newest_issued_date'], row['newest_filed_date']]
        valid_dates = []
        for date in dates:
            if not date:
                continue
            try:
                dt = datetime.fromisoformat(date.replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                valid_dates.append(dt)
            except (ValueError, TypeError):
                reasons.append('invalid_source_date')
        if not valid_dates or all((now-d).days > 14 for d in valid_dates):
            reasons.append('source_records_stale_or_missing')
        if any(d > now for d in valid_dates):
            reasons.append('future_source_date')
        item['health'] = 'attention' if reasons else 'current'
        item['reasons'] = reasons
        results.append(item)
    return {'observed_at': now.isoformat(), 'read_only': True,
            'attention_count': sum(r['health'] != 'current' for r in results),
            'jurisdictions': results,
            'notice': 'Fresh dates do not establish complete trade coverage or correct contractor identity.'}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', required=True)
    args = parser.parse_args()
    print(json.dumps(snapshot(args.db), indent=2))

if __name__ == '__main__':
    main()
