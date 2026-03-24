import httpx
from typing import Optional

SOCIALBLADE_BASE = "https://matrix.sbapis.com/b"


class SocialBladeClient:
    def __init__(self, client_id: str, token: str):
        self.client_id = client_id
        self.token = token

    @property
    def _headers(self):
        return {
            "clientid": self.client_id,
            "token": self.token,
        }

    async def get_channel_stats(self, channel_id: str) -> Optional[dict]:
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                resp = await client.get(
                    f"{SOCIALBLADE_BASE}/youtube/statistics",
                    params={"query": channel_id},
                    headers=self._headers,
                )
                if resp.status_code != 200:
                    return None

                data = resp.json()
                if not data.get('status', {}).get('success'):
                    return None

                sb = data.get('data', {})
                monthly = sb.get('statistics', {}).get('monthly', {})
                grade = sb.get('grade', {})
                ranks = sb.get('ranks', {})

                def extract(val):
                    """Handle both flat int and nested {'gained': int} formats."""
                    if isinstance(val, dict):
                        return val.get('gained') or val.get('total')
                    return val  # already an int

                return {
                    'monthly_sub_growth': extract(monthly.get('followers')),
                    'monthly_view_growth': extract(monthly.get('views')),
                    'grade': grade.get('grade') if isinstance(grade, dict) else grade,
                    'world_rank': (
                        ranks.get('subscribers', {}).get('world')
                        if isinstance(ranks, dict) else None
                    ),
                }
        except Exception as e:
            print(f"[SocialBlade] Error for {channel_id}: {e}")
            return None
