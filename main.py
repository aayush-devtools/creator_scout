import asyncio
import os
from datetime import datetime, timezone
from typing import Optional, List

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from database import init_db, get_conn, dict_cursor
from youtube_client import YouTubeClient
from socialblade_client import SocialBladeClient
from scorer import score_channel

load_dotenv()

youtube = YouTubeClient(os.getenv("YOUTUBE_API_KEY", ""))
socialblade = SocialBladeClient(
    os.getenv("SOCIALBLADE_CLIENT_ID", ""),
    os.getenv("SOCIALBLADE_TOKEN", ""),
)

app = FastAPI(title="DevTools Creator Scout")

try:
    app.mount("/static", StaticFiles(directory="frontend"), name="static")
except Exception:
    pass


@app.on_event("startup")
def on_startup():
    init_db()


@app.get("/")
async def root():
    return FileResponse("frontend/index.html")


# ── Partners ──────────────────────────────────────────────────────────────────

class PartnerCreate(BaseModel):
    channel_url: str
    notes: Optional[str] = None


@app.post("/api/partners")
async def add_partner(body: PartnerCreate):
    channel = await youtube.resolve_channel(body.channel_url)
    if not channel:
        raise HTTPException(400, "Could not resolve YouTube channel. Check the URL.")

    ch_id   = channel["id"]
    snippet = channel.get("snippet", {})
    stats   = channel.get("statistics", {})

    conn = get_conn()
    try:
        with dict_cursor(conn) as cur:
            cur.execute("""
                INSERT INTO partners (channel_id, channel_name, channel_url,
                    subscriber_count, thumbnail_url, notes, added_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (channel_id) DO UPDATE SET
                    channel_name     = EXCLUDED.channel_name,
                    subscriber_count = EXCLUDED.subscriber_count,
                    thumbnail_url    = EXCLUDED.thumbnail_url,
                    notes            = EXCLUDED.notes,
                    added_at         = EXCLUDED.added_at
            """, (
                ch_id,
                snippet.get("title", "Unknown"),
                f"https://youtube.com/channel/{ch_id}",
                int(stats.get("subscriberCount", 0)),
                snippet.get("thumbnails", {}).get("default", {}).get("url", ""),
                body.notes,
                datetime.utcnow().isoformat(),
            ))
        conn.commit()
    finally:
        conn.close()

    return {"success": True, "channel_name": snippet.get("title")}


@app.get("/api/partners")
async def list_partners():
    conn = get_conn()
    try:
        with dict_cursor(conn) as cur:
            cur.execute("SELECT * FROM partners ORDER BY subscriber_count DESC")
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


@app.delete("/api/partners/{channel_id}")
async def delete_partner(channel_id: str):
    conn = get_conn()
    try:
        with dict_cursor(conn) as cur:
            cur.execute("DELETE FROM partners WHERE channel_id = %s", (channel_id,))
        conn.commit()
    finally:
        conn.close()
    return {"success": True}


# ── Scouting ──────────────────────────────────────────────────────────────────

DEFAULT_KEYWORDS = [
    "programming tutorial",
    "developer tools review",
    "software engineering",
    "web development",
    "coding productivity",
    "tech stack explained",
]


class ScoutRequest(BaseModel):
    keywords: List[str] = DEFAULT_KEYWORDS
    results_per_keyword: int = 15
    min_subscribers: Optional[int] = None
    max_last_video_age_months: Optional[int] = None


def _is_recent_within_months(published_at_iso: str, months: int) -> bool:
    if not published_at_iso or months <= 0:
        return False
    try:
        published_at = datetime.fromisoformat(published_at_iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    days_old = (datetime.now(timezone.utc).date() - published_at.date()).days
    # Use a day-based inclusive window to avoid excluding edge cases by time-of-day.
    return days_old <= months * 31


@app.post("/api/scout")
async def run_scout(request: ScoutRequest):
    """
    Runs the full scout synchronously with concurrent API calls.
    Completes in ~15-20s (within Vercel's 60s limit).
    """
    conn = get_conn()
    try:
        # Prevent duplicate concurrent runs
        with dict_cursor(conn) as cur:
            cur.execute("SELECT id FROM scout_jobs WHERE status = 'running'")
            if cur.fetchone():
                raise HTTPException(409, "A scout job is already running.")

            cur.execute(
                "INSERT INTO scout_jobs (status, started_at, keywords) VALUES (%s, %s, %s) RETURNING id",
                ("running", datetime.utcnow().isoformat(), ", ".join(request.keywords)),
            )
            job_id = cur.fetchone()["id"]

            # Existing channel IDs to exclude
            cur.execute("SELECT channel_id FROM partners UNION SELECT channel_id FROM prospects")
            existing_ids = {r["channel_id"] for r in cur.fetchall()}

            cur.execute("SELECT * FROM partners")
            partners = [dict(r) for r in cur.fetchall()]
        conn.commit()
    except HTTPException:
        conn.close()
        raise
    except Exception as e:
        conn.close()
        raise HTTPException(500, str(e))

    processed = 0
    try:
        # ── Step 1: search YouTube for all keywords concurrently ──
        search_tasks = [
            youtube.search_channels(kw, request.results_per_keyword)
            for kw in request.keywords
        ]
        search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        seen: set = set(existing_ids)
        candidates = []
        for result in search_results:
            if isinstance(result, Exception):
                continue
            for ch in result:
                if ch["id"] not in seen:
                    candidates.append(ch)
                    seen.add(ch["id"])

        # ── Step 2: enrich all candidates concurrently (semaphore = 10) ──
        sem = asyncio.Semaphore(10)

        async def enrich(ch):
            async with sem:
                yt = await youtube.get_channel(ch["id"])
                sb = await socialblade.get_channel_stats(ch["id"])
                latest_video_published_at = await youtube.get_latest_video_published_at(ch["id"])
                return ch["id"], yt, sb, latest_video_published_at

        enriched_results = await asyncio.gather(
            *[enrich(ch) for ch in candidates], return_exceptions=True
        )

        # ── Step 3: score & save ──
        conn2 = get_conn()
        try:
            for item in enriched_results:
                if isinstance(item, Exception):
                    continue
                ch_id, yt, sb, latest_video_published_at = item
                if not yt:
                    continue

                snippet = yt.get("snippet", {})
                stats   = yt.get("statistics", {})
                subscriber_count = int(stats.get("subscriberCount", 0))

                if request.min_subscribers is not None and subscriber_count < request.min_subscribers:
                    continue

                if request.max_last_video_age_months is not None:
                    if not latest_video_published_at:
                        continue
                    if not _is_recent_within_months(
                        latest_video_published_at, request.max_last_video_age_months
                    ):
                        continue

                scores = score_channel(yt, sb, partners)

                with dict_cursor(conn2) as cur:
                    cur.execute("""
                        INSERT INTO prospects
                            (channel_id, channel_name, channel_url, subscriber_count, view_count,
                             video_count, description, thumbnail_url, country,
                             growth_score, engagement_score, similarity_score, priority_score,
                             monthly_sub_growth, sb_grade, status, discovered_at, last_video_published_at)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'new',%s,%s)
                        ON CONFLICT (channel_id) DO UPDATE SET
                            channel_name       = EXCLUDED.channel_name,
                            channel_url        = EXCLUDED.channel_url,
                            subscriber_count   = EXCLUDED.subscriber_count,
                            view_count         = EXCLUDED.view_count,
                            video_count        = EXCLUDED.video_count,
                            description        = EXCLUDED.description,
                            thumbnail_url      = EXCLUDED.thumbnail_url,
                            country            = EXCLUDED.country,
                            growth_score       = EXCLUDED.growth_score,
                            engagement_score   = EXCLUDED.engagement_score,
                            similarity_score   = EXCLUDED.similarity_score,
                            priority_score     = EXCLUDED.priority_score,
                            monthly_sub_growth = EXCLUDED.monthly_sub_growth,
                            sb_grade           = EXCLUDED.sb_grade,
                            last_video_published_at = COALESCE(
                                EXCLUDED.last_video_published_at,
                                prospects.last_video_published_at
                            )
                    """, (
                        ch_id,
                        snippet.get("title", ""),
                        f"https://youtube.com/channel/{ch_id}",
                        subscriber_count,
                        int(stats.get("viewCount", 0)),
                        int(stats.get("videoCount", 0)),
                        snippet.get("description", "")[:500],
                        snippet.get("thumbnails", {}).get("default", {}).get("url", ""),
                        snippet.get("country", ""),
                        scores["growth_score"],
                        scores["engagement_score"],
                        scores["similarity_score"],
                        scores["priority_score"],
                        sb.get("monthly_sub_growth") if sb else None,
                        sb.get("grade") if sb else None,
                        datetime.utcnow().isoformat(),
                        latest_video_published_at,
                    ))
                conn2.commit()
                processed += 1
        finally:
            conn2.close()

        # Mark job done
        conn3 = get_conn()
        try:
            with dict_cursor(conn3) as cur:
                cur.execute(
                    "UPDATE scout_jobs SET status='completed', completed_at=%s, channels_found=%s WHERE id=%s",
                    (datetime.utcnow().isoformat(), processed, job_id),
                )
            conn3.commit()
        finally:
            conn3.close()

    except Exception as e:
        conn4 = get_conn()
        try:
            with dict_cursor(conn4) as cur:
                cur.execute(
                    "UPDATE scout_jobs SET status='failed', error=%s WHERE id=%s",
                    (str(e), job_id),
                )
            conn4.commit()
        finally:
            conn4.close()
        raise HTTPException(500, f"Scout failed: {e}")

    return {"job_id": job_id, "status": "completed", "channels_found": processed}


@app.get("/api/scout/status")
async def scout_status():
    conn = get_conn()
    try:
        with dict_cursor(conn) as cur:
            cur.execute("SELECT * FROM scout_jobs ORDER BY id DESC LIMIT 1")
            row = cur.fetchone()
            return dict(row) if row else {"status": "none"}
    finally:
        conn.close()


# ── Prospects ─────────────────────────────────────────────────────────────────

@app.get("/api/prospects")
async def list_prospects(status: Optional[str] = None, min_score: Optional[float] = None):
    conn = get_conn()
    try:
        with dict_cursor(conn) as cur:
            query = "SELECT * FROM prospects WHERE 1=1"
            params: list = []
            if status:
                query += " AND status = %s"
                params.append(status)
            if min_score is not None:
                query += " AND priority_score >= %s"
                params.append(min_score)
            query += " ORDER BY priority_score DESC"
            cur.execute(query, params)
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


@app.api_route("/api/prospects/backfill-last-video", methods=["GET", "POST"])
async def backfill_last_video(limit: int = Query(40, ge=1, le=120)):
    """
    Fetches latest public video timestamp from YouTube for prospects missing
    last_video_published_at. Scout only touches channels returned by search;
    this fills the gap for older rows so activity filters work.
    """
    conn = get_conn()
    try:
        with dict_cursor(conn) as cur:
            cur.execute(
                """
                SELECT channel_id FROM prospects
                WHERE last_video_published_at IS NULL OR last_video_published_at = ''
                ORDER BY priority_score DESC NULLS LAST
                LIMIT %s
                """,
                (limit,),
            )
            channel_ids = [r["channel_id"] for r in cur.fetchall()]
    finally:
        conn.close()

    sem = asyncio.Semaphore(6)

    async def fetch_one(cid: str):
        async with sem:
            return cid, await youtube.get_latest_video_published_at(cid)

    results = await asyncio.gather(
        *[fetch_one(cid) for cid in channel_ids], return_exceptions=True
    )

    updated = 0
    conn2 = get_conn()
    try:
        with dict_cursor(conn2) as cur:
            for item in results:
                if isinstance(item, Exception):
                    continue
                cid, published_at = item
                if not published_at:
                    continue
                cur.execute(
                    """
                    UPDATE prospects SET last_video_published_at = %s
                    WHERE channel_id = %s
                    AND (last_video_published_at IS NULL OR last_video_published_at = '')
                    """,
                    (published_at, cid),
                )
                updated += cur.rowcount
        conn2.commit()
    finally:
        conn2.close()

    conn3 = get_conn()
    try:
        with dict_cursor(conn3) as cur:
            cur.execute(
                """
                SELECT COUNT(*) AS cnt FROM prospects
                WHERE last_video_published_at IS NULL OR last_video_published_at = ''
                """
            )
            remaining = cur.fetchone()["cnt"]
    finally:
        conn3.close()

    return {
        "attempted": len(channel_ids),
        "updated": updated,
        "remaining_without_date": remaining,
    }


class StatusUpdate(BaseModel):
    status: str


@app.patch("/api/prospects/{channel_id}/status")
async def update_status(channel_id: str, body: StatusUpdate):
    valid = {"new", "monitor", "outreach", "pass"}
    if body.status not in valid:
        raise HTTPException(400, f"status must be one of {sorted(valid)}")
    conn = get_conn()
    try:
        with dict_cursor(conn) as cur:
            cur.execute(
                "UPDATE prospects SET status = %s WHERE channel_id = %s",
                (body.status, channel_id),
            )
        conn.commit()
    finally:
        conn.close()
    return {"success": True}


@app.delete("/api/prospects/{channel_id}")
async def delete_prospect(channel_id: str):
    conn = get_conn()
    try:
        with dict_cursor(conn) as cur:
            cur.execute("DELETE FROM prospects WHERE channel_id = %s", (channel_id,))
        conn.commit()
    finally:
        conn.close()
    return {"success": True}


# ── Stats ─────────────────────────────────────────────────────────────────────

@app.get("/api/stats")
async def get_stats():
    conn = get_conn()
    try:
        today = datetime.utcnow().date().isoformat()
        with dict_cursor(conn) as cur:
            cur.execute("""
                SELECT
                    COUNT(*) AS total,
                    SUM(CASE WHEN priority_score >= 70 THEN 1 ELSE 0 END) AS high_priority,
                    SUM(CASE WHEN status = 'outreach' THEN 1 ELSE 0 END) AS outreach,
                    SUM(CASE WHEN status = 'monitor'  THEN 1 ELSE 0 END) AS monitor,
                    SUM(CASE WHEN discovered_at LIKE %s THEN 1 ELSE 0 END) AS new_today
                FROM prospects
            """, (f"{today}%",))
            row = dict(cur.fetchone())

            cur.execute("SELECT COUNT(*) AS cnt FROM partners")
            row["partners"] = cur.fetchone()["cnt"]
        return row
    finally:
        conn.close()
