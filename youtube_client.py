import httpx
import re
from typing import Optional, List

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
                "part": "snippet,statistics",
                "id": channel_id,
                "key": self.api_key,
            })
            data = resp.json()
            items = data.get('items', [])
            return items[0] if items else None

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
