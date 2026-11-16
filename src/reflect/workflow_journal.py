"""Recover filesystem actions against their committed intervention identity."""
from __future__ import annotations

import json
import os
from contextlib import closing
from pathlib import Path
from uuid import uuid4

from reflect.store.sqlite import connect_sqlite_read_only


class WorkflowFileJournal:
    def __init__(self, database: Path):
        self.database = database
        self.directory = database.with_name(database.name + '.workflow-actions')

    @classmethod
    def for_connection(cls, conn):
        filename = next((row[2] for row in conn.execute('PRAGMA database_list') if row[1] == 'main'), '')
        return cls(Path(filename)) if filename else None

    def begin(self, *, target: Path, before: str | None, after: str | None,
              intervention_id: str, operation: str) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        record = self.directory / (uuid4().hex + '.json')
        data = {'target': str(target), 'before': before, 'after': after,
                'intervention_id': intervention_id, 'operation': operation}
        temporary = record.with_suffix('.tmp')
        with temporary.open('x', encoding='utf-8') as handle:
            os.chmod(temporary, 0o600)
            json.dump(data, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, record)
        self._sync()
        return record

    def _sync(self):
        fd = os.open(self.directory, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def finish(self, record: Path):
        record.unlink(missing_ok=True)
        self._sync()

    def recover(self) -> list[str]:
        """Undo only an uncommitted exact write; never replay an approval."""
        if not self.directory.exists():
            return []
        from reflect.improvements.workflows import WorkflowService
        conflicts = []
        with closing(connect_sqlite_read_only(self.database, profile=None)) as conn:
            for record in self.directory.glob('*.json'):
                data = json.loads(record.read_text())
                target = Path(data['target'])
                actual = target.read_text() if target.exists() else None
                row = conn.execute('SELECT status FROM interventions WHERE id=?', (data['intervention_id'],)).fetchone()
                committed = row and row[0] == ('active' if data['operation'] == 'apply' else 'rolled_back')
                expected = data['after'] if committed else data['before']
                if actual == expected:
                    self.finish(record)
                elif not committed and actual == data['after']:
                    WorkflowService._restore_file(target, data['before'])
                    self.finish(record)
                else:
                    conflicts.append(str(target))
        return conflicts
