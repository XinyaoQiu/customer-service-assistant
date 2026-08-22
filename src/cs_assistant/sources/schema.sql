-- Publisher-facing business data.
--
-- Review outcomes are a code and nothing else, the way an HTTP status is. The message a
-- publisher reads is a fixed string the backend owns (see reasons.py); it is not stored
-- per row, and there is no free-text reviewer note to leak.

CREATE TABLE IF NOT EXISTS publishers (
    publisher_id   VARCHAR(32) PRIMARY KEY,
    display_name   VARCHAR(128) NOT NULL,
    tier           VARCHAR(16) NOT NULL DEFAULT 'standard',
    locale         VARCHAR(8)  NOT NULL DEFAULT 'en',
    joined_at      DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS articles (
    article_id     VARCHAR(32) PRIMARY KEY,
    publisher_id   VARCHAR(32) NOT NULL,
    title          VARCHAR(255) NOT NULL,
    submitted_at   DATETIME NOT NULL,
    published_at   DATETIME NULL,
    category       VARCHAR(32),
    INDEX idx_publisher_time (publisher_id, submitted_at DESC)
);

CREATE TABLE IF NOT EXISTS article_reviews (
    article_id     VARCHAR(32) PRIMARY KEY,
    status         VARCHAR(24) NOT NULL,
    reason_code    VARCHAR(40) NULL,
    reviewed_at    DATETIME NULL,
    appealable     BOOLEAN NOT NULL DEFAULT FALSE
);

-- Reach data. Ranking signals are deliberately absent: the assistant may report what a
-- publisher's own content did, never why the ranker chose it.
CREATE TABLE IF NOT EXISTS article_stats (
    article_id     VARCHAR(32) PRIMARY KEY,
    impressions    INT NOT NULL DEFAULT 0,
    clicks         INT NOT NULL DEFAULT 0,
    measured_at    DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS escalations (
    ticket_id      VARCHAR(64) PRIMARY KEY,
    publisher_id   VARCHAR(32) NOT NULL,
    article_id     VARCHAR(32) NULL,
    reason_code    VARCHAR(40) NULL,
    publisher_message TEXT NOT NULL,
    transcript     TEXT NULL,
    created_at     DATETIME NOT NULL,
    -- Resuming an interrupt re-runs the node from its start, so one escalation can be
    -- attempted twice. The unique key makes the second attempt a lookup.
    idempotency_key VARCHAR(128) NOT NULL,
    UNIQUE KEY uniq_idem (idempotency_key)
);
