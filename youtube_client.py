import httpx
import re
from typing import Optional, List

_SOCIAL_PATTERNS = [
    ("github",   r'github\.com/([A-Za-z0-9_.\-]+)'),
    ("twitter",  r'(?:twitter|x)\.com/([A-Za-z0-9_]+)'),
    ("linkedin", r'linkedin\.com/in/([A-Za-z0-9_\-]+)'),
    ("website",  r'https?://(?!(?:www\.)?(?:youtube|youtu\.be|twitter|x\.com|github|linkedin))[\w\-]+\.[\w\-./]+'),
]


def extract_social_links(text: str) -> dict:
    """Extract social profile links from a block of text."""
    found = {}
    for key, pattern in _SOCIAL_PATTERNS:
        m = re.search(pattern, text or "")
        if m:
            found[key] = m.group(0)
    return found

YOUTUBE_API_BASE = "https://www.googleapis.com/youtube/v3"


class YouTubeClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def extract_channel_identifier(self, url: str) -> tuple[Optional[str], str]:
        """Returns (identifier, type) where type is 'id', 'handle', 'username', or 'custom'"""
        url = url.strip()

        match = re.search(r'youtube\.com/channel/([a-zA-Z0-9_-]+)', url)
        if match:
            return match.group(1), 'id'

        match = re.search(r'youtube\.com/@([a-zA-Z0-9_.\-]+)', url)
        if match:
            return match.group(1), 'handle'

        match = re.search(r'youtube\.com/c/([a-zA-Z0-9_\-]+)', url)
        if match:
            return match.group(1), 'custom'

        match = re.search(r'youtube\.com/user/([a-zA-Z0-9_\-]+)', url)
        if match:
            return match.group(1), 'username'

        # bare @handle
        if url.startswith('@'):
            return url[1:], 'handle'

        return None, 'unknown'

    async def resolve_channel(self, url: str) -> Optional[dict]:
        identifier, id_type = self.extract_channel_identifier(url)
        if not identifier:
            return None

        params = {
            "part": "snippet,statistics",
            "key": self.api_key,
        }

        if id_type == 'id':
            params['id'] = identifier
        elif id_type == 'handle':
            params['forHandle'] = identifier
        elif id_type == 'username':
            params['forUsername'] = identifier
        else:
            params['forHandle'] = identifier

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{YOUTUBE_API_BASE}/channels", params=params)
            data = resp.json()
            items = data.get('items', [])
            return items[0] if items else None

    async def get_channel(self, channel_id: str) -> Optional[dict]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{YOUTUBE_API_BASE}/channels", params={
                "part": "snippet,statistics,contentDetails,brandingSettings",
                "id": channel_id,
                "key": self.api_key,
            })
            data = resp.json()
            items = data.get('items', [])
            return items[0] if items else None

    async def get_recent_videos_with_stats(self, channel_id: str, count: int = 5) -> list:
        """Fetch last N videos for a channel with view/like/comment stats."""
        async with httpx.AsyncClient(timeout=20.0) as client:
            # Get uploads playlist ID
            ch_resp = await client.get(f"{YOUTUBE_API_BASE}/channels", params={
                "part": "contentDetails",
                "id": channel_id,
                "key": self.api_key,
            })
            ch_data = ch_resp.json()
            ch_items = ch_data.get("items", [])
            if not ch_items:
                return []
            uploads_playlist = (
                ch_items[0]
                .get("contentDetails", {})
                .get("relatedPlaylists", {})
                .get("uploads")
            )
            if not uploads_playlist:
                return []

            # Get last N video IDs from uploads playlist
            pl_resp = await client.get(f"{YOUTUBE_API_BASE}/playlistItems", params={
                "part": "contentDetails,snippet",
                "playlistId": uploads_playlist,
                "maxResults": count,
                "key": self.api_key,
            })
            pl_data = pl_resp.json()
            pl_items = pl_data.get("items", [])
            if not pl_items:
                return []

            video_ids = [item["contentDetails"]["videoId"] for item in pl_items]

            # Batch get video statistics + snippet
            v_resp = await client.get(f"{YOUTUBE_API_BASE}/videos", params={
                "part": "statistics,snippet",
                "id": ",".join(video_ids),
                "key": self.api_key,
            })
            v_data = v_resp.json()

            videos = []
            for v in v_data.get("items", []):
                snippet = v.get("snippet", {})
                stats = v.get("statistics", {})
                videos.append({
                    "video_id": v["id"],
                    "title": snippet.get("title", ""),
                    "published_at": snippet.get("publishedAt"),
                    "thumbnail_url": (
                        snippet.get("thumbnails", {})
                        .get("medium", {})
                        .get("url", "")
                        or snippet.get("thumbnails", {})
                        .get("default", {})
                        .get("url", "")
                    ),
                    "view_count": int(stats.get("viewCount", 0)),
                    "like_count": int(stats.get("likeCount", 0)),
                    "comment_count": int(stats.get("commentCount", 0)),
                })

            videos.sort(key=lambda v: v["published_at"] or "", reverse=True)
            return videos[:count]

    async def search_channels(self, query: str, max_results: int = 25) -> List[dict]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{YOUTUBE_API_BASE}/search", params={
                "part": "snippet",
                "q": query,
                "type": "channel",
                "maxResults": min(max_results, 50),
                "relevanceLanguage": "en",
                "key": self.api_key,
            })
            data = resp.json()
            channels = []
            for item in data.get('items', []):
                channels.append({
                    'id': item['id']['channelId'],
                    'title': item['snippet']['title'],
                    'description': item['snippet'].get('description', ''),
                })
            return channels

    async def get_latest_video_published_at(self, channel_id: str) -> Optional[str]:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(f"{YOUTUBE_API_BASE}/search", params={
                "part": "snippet",
                "channelId": channel_id,
                "type": "video",
                "order": "date",
                "maxResults": 1,
                "key": self.api_key,
            })
            data = resp.json()
            items = data.get("items", [])
            if not items:
                return None
            return items[0].get("snippet", {}).get("publishedAt")
