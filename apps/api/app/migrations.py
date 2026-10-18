"""Small, additive schema versioning while this project remains SQLite-first."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, MetaData, Table, inspect, select
from sqlalchemy.engine import Engine


CURRENT_SCHEMA_VERSION = 14


def upgrade_schema(engine: Engine, metadata: MetaData) -> None:
    # create_all only creates missing tables; it does not rewrite existing phase-1 tables.
    metadata.create_all(engine)
    if not inspect(engine).has_table("schema_migrations"):
        Table("schema_migrations", MetaData(),
              Column("version", Integer, primary_key=True),
              Column("applied_at", DateTime(timezone=True), nullable=False)).create(engine)
    # Preserve existing QA history while making the exact retrieval rewrite auditable.
    if inspect(engine).has_table("qa_runs") and "retrieval_query" not in {column["name"] for column in inspect(engine).get_columns("qa_runs")}:
        with engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE qa_runs ADD COLUMN retrieval_query TEXT")
    if inspect(engine).has_table("qa_runs"):
        existing = {column["name"] for column in inspect(engine).get_columns("qa_runs")}
        with engine.begin() as connection:
            for name in ("baseline_answer", "support_trace"):
                if name not in existing:
                    connection.exec_driver_sql(f"ALTER TABLE qa_runs ADD COLUMN {name} TEXT")
            if "parse_snapshot_ids" not in existing:
                connection.exec_driver_sql("ALTER TABLE qa_runs ADD COLUMN parse_snapshot_ids JSON")
    if inspect(engine).has_table("evidence_notes"):
        note_columns = {column["name"]: column for column in inspect(engine).get_columns("evidence_notes")}
        source_nullable = note_columns.get("source_qa_run_id", {}).get("nullable", False)
        if not source_nullable or "paper_ids_snapshot" not in note_columns:
            with engine.begin() as connection:
                connection.exec_driver_sql("ALTER TABLE evidence_notes RENAME TO evidence_notes_v9")
                connection.exec_driver_sql("""
                    CREATE TABLE evidence_notes (
                        id VARCHAR(80) NOT NULL PRIMARY KEY,
                        title TEXT NOT NULL,
                        body TEXT NOT NULL,
                        source_qa_run_id VARCHAR(80),
                        source_query_snapshot TEXT NOT NULL,
                        claims_snapshot JSON NOT NULL,
                        citations_snapshot JSON NOT NULL,
                        paper_ids_snapshot JSON NOT NULL DEFAULT '[]',
                        created_at DATETIME NOT NULL,
                        updated_at DATETIME NOT NULL
                    )
                """)
                connection.exec_driver_sql("""
                    INSERT INTO evidence_notes
                      (id,title,body,source_qa_run_id,source_query_snapshot,claims_snapshot,
                       citations_snapshot,paper_ids_snapshot,created_at,updated_at)
                    SELECT id,title,body,source_qa_run_id,source_query_snapshot,claims_snapshot,
                           citations_snapshot,'[]',created_at,updated_at
                    FROM evidence_notes_v9
                """)
                connection.exec_driver_sql("DROP TABLE evidence_notes_v9")
                connection.exec_driver_sql(
                    "CREATE INDEX ix_evidence_notes_source_qa_run_id ON evidence_notes (source_qa_run_id)")
    if inspect(engine).has_table("papers"):
        paper_columns = {column["name"] for column in inspect(engine).get_columns("papers")}
        with engine.begin() as connection:
            if "source" not in paper_columns:
                connection.exec_driver_sql("ALTER TABLE papers ADD COLUMN source VARCHAR(30) NOT NULL DEFAULT 'manifest'")
            if "original_filename" not in paper_columns:
                connection.exec_driver_sql("ALTER TABLE papers ADD COLUMN original_filename TEXT")
            if "status" not in paper_columns:
                connection.exec_driver_sql("ALTER TABLE papers ADD COLUMN status VARCHAR(30) NOT NULL DEFAULT 'ready'")
    if inspect(engine).has_table("operation_tasks"):
        task_columns = {column["name"] for column in inspect(engine).get_columns("operation_tasks")}
        if "phase_trace" not in task_columns:
            with engine.begin() as connection:
                connection.exec_driver_sql("ALTER TABLE operation_tasks ADD COLUMN phase_trace JSON")
    table = Table("schema_migrations", MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        applied = set(connection.execute(select(table.c.version)).scalars())
        if CURRENT_SCHEMA_VERSION not in applied:
            connection.execute(table.insert().values(version=CURRENT_SCHEMA_VERSION,
                                                       applied_at=datetime.now(timezone.utc)))
