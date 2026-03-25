import math
import json
from datetime import datetime
from typing import Optional, List


def compute_roi_metrics(videos: list, subscriber_count: int) -> dict:
    """Compute ROI-related metrics from recent video performance data."""
    if not videos:
        return {
            "avg_views_last5": None,
            "avg_likes_last5": None,
            "avg_comments_last5": None,
            "upload_frequency_days": None,
            "roi_score": 0.0,
        }

    avg_views    = sum(v["view_count"]   for v in videos) / len(videos)
    avg_likes    = sum(v["like_count"]   for v in videos) / len(videos)
    avg_comments = sum(v["comment_count"] for v in videos) / len(videos)

    # Upload frequency: avg days between consecutive videos
    upload_frequency_days = None
    dates = sorted(
        [v["published_at"] for v in videos if v.get("published_at")],
        reverse=True,
    )
    if len(dates) >= 2:
        try:
            parsed = [datetime.fromisoformat(d.replace("Z", "+00:00")) for d in dates]
            gaps = [(parsed[i] - parsed[i + 1]).days for i in range(len(parsed) - 1)]
            upload_frequency_days = sum(gaps) / len(gaps)
        except Exception:
            pass

    # ── ROI Score (0–100) ────────────────────────────────────────────────────
    roi_score = 0.0
    if subscriber_count > 0 and avg_views > 0:
        # 1. View rate: % of subscribers who watch recent videos (20% = excellent)
        view_rate = avg_views / subscriber_count
        view_rate_score = min(100.0, (view_rate / 0.20) * 100)

        # 2. Engagement rate: (likes + comments) / views (4% = excellent)
        eng_rate = (avg_likes + avg_comments) / avg_views if avg_views else 0
        eng_score = min(100.0, (eng_rate / 0.04) * 100)

        # 3. Upload frequency score (weekly = 100, monthly = 50, quarterly = 10)
        freq_score = 0.0
        if upload_frequency_days is not None:
            if upload_frequency_days <= 7:
                freq_score = 100.0
            elif upload_frequency_days <= 30:
                freq_score = 50.0 + (30 - upload_frequency_days) / 23 * 50
            elif upload_frequency_days <= 90:
                freq_score = 10.0 + (90 - upload_frequency_days) / 60 * 40
            else:
                freq_score = max(0.0, 10.0 - (upload_frequency_days - 90) / 30)

        roi_score = (
            view_rate_score * 0.50
            + eng_score      * 0.30
            + freq_score     * 0.20
        )

    return {
        "avg_views_last5":       round(avg_views),
        "avg_likes_last5":       round(avg_likes),
        "avg_comments_last5":    round(avg_comments),
        "upload_frequency_days": round(upload_frequency_days, 1) if upload_frequency_days is not None else None,
        "roi_score":             round(roi_score, 1),
    }


def score_channel(yt_data: dict, sb_data: Optional[dict], partners: List[dict]) -> dict:
    stats = yt_data.get('statistics', {})

    subscriber_count = int(stats.get('subscriberCount', 0))
    view_count = int(stats.get('viewCount', 0))
    video_count = max(int(stats.get('videoCount', 1)), 1)

    # ── Growth Score (0–100) ──────────────────────────────────────────────────
    # Based on monthly subscriber growth rate from SocialBlade
    growth_score = 0.0
    if sb_data and sb_data.get('monthly_sub_growth') and subscriber_count > 0:
        monthly_growth = sb_data['monthly_sub_growth']
        if monthly_growth > 0:
            growth_rate_pct = (monthly_growth / subscriber_count) * 100
            # 3%+ monthly = perfect score; linear below that
            growth_score = min(100.0, growth_rate_pct * (100 / 3))

    # ── Engagement Score (0–100) ──────────────────────────────────────────────
    # Avg views per video vs subscriber count
    engagement_score = 0.0
    if subscriber_count > 0:
        avg_views = view_count / video_count
        ratio = avg_views / subscriber_count  # e.g. 0.5 = 50% view-to-sub
        # 30%+ ratio = excellent engagement for tech channels
        engagement_score = min(100.0, (ratio / 0.30) * 100)

    # ── Size Score (0–100) ────────────────────────────────────────────────────
    # Sweet spot for tech/devtools: 50k–2M subs; peak at ~200k
    size_score = 0.0
    if subscriber_count >= 5_000:
        log_val = math.log10(subscriber_count)
        log_min = math.log10(5_000)
        log_peak = math.log10(200_000)
        log_max = math.log10(5_000_000)

        if log_val <= log_peak:
            size_score = ((log_val - log_min) / (log_peak - log_min)) * 100
        else:
            size_score = max(30.0, 100 - ((log_val - log_peak) / (log_max - log_peak)) * 70)

    # ── Similarity Score (0–100) ──────────────────────────────────────────────
    # How close is this channel to the size of existing top partners?
    similarity_score = 50.0
    if partners:
        sub_counts = [p['subscriber_count'] for p in partners if p.get('subscriber_count', 0) > 0]
        if sub_counts and subscriber_count > 0:
            avg_partner_subs = sum(sub_counts) / len(sub_counts)
            ratio = subscriber_count / avg_partner_subs
            # Ideal band: 0.3x–5x of avg partner size
            if 0.3 <= ratio <= 5.0:
                deviation = abs(math.log(ratio))   # 0 = perfect match
                similarity_score = max(40.0, 100.0 - deviation * 30)
            elif ratio < 0.3:
                similarity_score = max(10.0, 40.0 * (ratio / 0.3))
            else:
                similarity_score = max(10.0, 40.0 * (5.0 / ratio))

    # ── Priority Score: weighted combination ──────────────────────────────────
    priority_score = (
        growth_score * 0.35
        + engagement_score * 0.25
        + size_score * 0.25
        + similarity_score * 0.15
    )

    return {
        'growth_score': round(growth_score, 1),
        'engagement_score': round(engagement_score, 1),
        'similarity_score': round(similarity_score, 1),
        'priority_score': round(priority_score, 1),
    }
