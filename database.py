import os
import psycopg2
import psycopg2.extras


def _connect():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL environment variable not set")
    return psycopg2.connect(url)


def init_db():
    with _connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS partners (
                    channel_id  TEXT PRIMARY KEY,
                    channel_name TEXT NOT NULL,
                    channel_url  TEXT,
                    subscriber_count INTEGER,
                    thumbnail_url TEXT,
                    notes TEXT,
                    added_at TEXT
                );

                CREATE TABLE IF NOT EXISTS prospects (
                    channel_id   TEXT PRIMARY KEY,
                    channel_name TEXT NOT NULL,
                    channel_url  TEXT,
                    subscriber_count INTEGER,
                    view_count   INTEGER,
                    video_count  INTEGER,
                    description  TEXT,
                    thumbnail_url TEXT,
                    country      TEXT,
                    growth_score     REAL DEFAULT 0,
                    engagement_score REAL DEFAULT 0,
                    similarity_score REAL DEFAULT 0,
                    priority_score   REAL DEFAULT 0,
                    monthly_sub_growth INTEGER,
                    sb_grade TEXT,
                    status TEXT DEFAULT 'new',
                    discovered_at TEXT,
                    last_video_published_at TEXT
                );

                CREATE TABLE IF NOT EXISTS scout_jobs (
                    id           SERIAL PRIMARY KEY,
                    status       TEXT,
                    started_at   TEXT,
                    completed_at TEXT,
                    keywords     TEXT,
                    channels_found INTEGER DEFAULT 0,
                    error        TEXT
                );
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS channel_recent_videos (
                    id           SERIAL PRIMARY KEY,
                    channel_id   TEXT NOT NULL,
                    video_id     TEXT NOT NULL,
                    title        TEXT,
                    published_at TEXT,
                    view_count   INTEGER DEFAULT 0,
                    like_count   INTEGER DEFAULT 0,
                    comment_count INTEGER DEFAULT 0,
                    thumbnail_url TEXT,
                    fetched_at   TEXT,
                    UNIQUE(channel_id, video_id)
                );
            """)

            # Backfill columns for existing databases.
            for col in [
                "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS last_video_published_at TEXT",
                "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS avg_views_last5 INTEGER",
                "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS avg_likes_last5 INTEGER",
                "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS avg_comments_last5 INTEGER",
                "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS upload_frequency_days REAL",
                "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS roi_score REAL DEFAULT 0",
                "ALTER TABLE prospects ADD COLUMN IF NOT EXISTS social_links TEXT",
            ]:
                cur.execute(col)


def get_conn():
    """Return a psycopg2 connection with RealDictCursor as default cursor."""
    conn = _connect()
    return conn


def dict_cursor(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
