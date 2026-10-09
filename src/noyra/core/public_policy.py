"""Public admission controls migration; preserve historical control hashes."""

MIGRATION_V82 = """
CREATE TABLE public_post_controls_v82 (
 subject_id TEXT PRIMARY KEY REFERENCES subject_identity(subject_id),
 rate_limit_per_hour INTEGER NOT NULL CHECK(rate_limit_per_hour BETWEEN 1 AND 100000),
 queue_cap INTEGER NOT NULL CHECK(queue_cap BETWEEN 1 AND 1000000),
 captcha_ttl_seconds INTEGER NOT NULL CHECK(captcha_ttl_seconds BETWEEN 30 AND 3600),
 captcha_max_attempts INTEGER NOT NULL CHECK(captcha_max_attempts BETWEEN 1 AND 20),
 captcha_mode TEXT NOT NULL CHECK(captcha_mode IN ('letters','digits','alphanumeric')),
 updated_at TEXT NOT NULL, updated_by TEXT NOT NULL, state_hash TEXT NOT NULL,
 storage_cap_bytes INTEGER NOT NULL DEFAULT 250000000
 CHECK(storage_cap_bytes BETWEEN 1000 AND 2000000000),
 captcha_issue_limit_per_hour INTEGER NOT NULL DEFAULT 30
 CHECK(captcha_issue_limit_per_hour BETWEEN 1 AND 100000),
 captcha_global_rate_per_minute INTEGER NOT NULL DEFAULT 300
 CHECK(captcha_global_rate_per_minute BETWEEN 1 AND 100000)
);
INSERT INTO public_post_controls_v82 SELECT * FROM public_post_controls;
DROP TABLE public_post_controls;
ALTER TABLE public_post_controls_v82 RENAME TO public_post_controls;
CREATE TABLE IF NOT EXISTS public_post_length_policies (
 subject_id TEXT PRIMARY KEY REFERENCES subject_identity(subject_id),
 max_content_chars INTEGER NOT NULL CHECK(max_content_chars BETWEEN 1 AND 20000),
 revision INTEGER NOT NULL CHECK(revision >= 1), updated_at TEXT NOT NULL,
 updated_by TEXT NOT NULL, state_hash TEXT NOT NULL
);
"""
