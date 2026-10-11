"""Attempt-local evidence; migration never guesses historical usage or prices."""

ATTEMPT_USAGE_COLUMNS = {
    "usage_source": "TEXT NOT NULL DEFAULT 'legacy' "
    "CHECK(usage_source IN ('legacy','unknown','provider','estimated'))",
    "pricing_snapshot_json": "TEXT",
}

MIGRATION_V91 = """
CREATE TRIGGER IF NOT EXISTS model_attempt_pricing_immutable
BEFORE UPDATE OF pricing_snapshot_json ON model_attempts
WHEN NEW.pricing_snapshot_json IS NOT OLD.pricing_snapshot_json BEGIN
    SELECT RAISE(ABORT,'attempt pricing evidence is immutable');
END;
CREATE TRIGGER IF NOT EXISTS model_attempt_usage_source_immutable
BEFORE UPDATE OF usage_source ON model_attempts
WHEN OLD.status NOT IN ('authorized','executing')
 AND NEW.usage_source IS NOT OLD.usage_source BEGIN
    SELECT RAISE(ABORT,'terminal attempt usage evidence is immutable');
END;
"""
