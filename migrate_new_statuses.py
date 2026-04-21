"""
Запускать ОДИН раз после деплоя для добавления новых значений enum в PostgreSQL.
python migrate_new_statuses.py
"""
from database import SyncSessionLocal

SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_enum
        WHERE enumlabel = 'needs_attention'
          AND enumtypid = (SELECT oid FROM pg_type WHERE typname = 'servicestatus')
    ) THEN
        ALTER TYPE servicestatus ADD VALUE 'needs_attention';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_enum
        WHERE enumlabel = 'operator_requested'
          AND enumtypid = (SELECT oid FROM pg_type WHERE typname = 'servicestatus')
    ) THEN
        ALTER TYPE servicestatus ADD VALUE 'operator_requested';
    END IF;
END$$;
"""

with SyncSessionLocal() as db:
    db.execute(__import__('sqlalchemy').text(SQL))
    db.commit()
    print("OK: новые значения enum добавлены в БД")
