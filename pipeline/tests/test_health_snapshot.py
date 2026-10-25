import sqlite3
from datetime import datetime, timezone
import pytest
from pipeline.health_snapshot import snapshot


def test_missing_database_does_not_get_created(tmp_path):
    path = tmp_path / 'missing.db'
    with pytest.raises(sqlite3.OperationalError):
        snapshot(path)
    assert not path.exists()


def test_stale_source_is_not_healthy_after_successful_refresh(tmp_path):
    path = tmp_path / 'source.db'
    c = sqlite3.connect(path)
    c.executescript('''CREATE TABLE jurisdictions(slug,name,status,last_synced_at,last_sync_status);
        CREATE TABLE permits(id,jurisdiction,issued_date,filed_date);
        INSERT INTO jurisdictions VALUES ('a','A','connected','2026-10-25T00:00:00Z','success');
        INSERT INTO permits VALUES (1,'a','2025-01-01',NULL);''')
    c.commit()
    c.close()
    before = path.read_bytes()
    result = snapshot(path, now=datetime(2026,10,25,1,tzinfo=timezone.utc))
    assert result['attention_count'] == 1
    assert 'source_records_stale_or_missing' in result['jurisdictions'][0]['reasons']
    assert path.read_bytes() == before
