"""ESS API 客户端 - 登录、查询、详情拉取"""
import sys
import requests
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict, Optional, Callable
from ..config import get_settings
from ..utils.logger import get_logger

# 引入 keycloak-auth skill
import os
_KEYCLOAK_SKILL = "C:/Users/junhao.niu/.claude/skills/keycloak-auth/scripts"
_LOCAL_KEYCLOAK = os.path.join(os.path.dirname(__file__), "keycloak_auth.py")
if os.path.exists(_LOCAL_KEYCLOAK):
    from .keycloak_auth import get_token as _keycloak_get_token
else:
    if _KEYCLOAK_SKILL not in sys.path:
        sys.path.insert(0, _KEYCLOAK_SKILL)
    from keycloak_auth import get_token as _keycloak_get_token

logger = get_logger(__name__)
settings = get_settings()


class ESSClient:
    """ESS API 客户端（封装登录、查询、详情拉取）"""

    def __init__(self, token: Optional[str] = None):
        self.token = token
        self.base_url = settings.ESS_BASE_URL

    def login(self, username: str, password: str) -> str:
        """通过 Keycloak 获取 ESS token（不再使用 Playwright）"""
        token = _keycloak_get_token(username=username, password=password, force_refresh=True)
        self.token = token
        logger.info(f"ESS login successful via Keycloak, token length: {len(token)}")
        return token

    def fetch_events(
        self,
        plate: str,
        start_time: str,
        end_time: str,
        progress_callback: Optional[Callable[[int, Optional[int], str], None]] = None
    ) -> List[Dict]:
        """
        查询事件列表并并发拉取详情

        Args:
            plate: 车牌号，例如 "IP5PM-4032"
            start_time: 开始时间，格式 "YYYY-MM-DD HH:MM:SS"
            end_time: 结束时间，格式 "YYYY-MM-DD HH:MM:SS"
            progress_callback: 可选的进度回调函数 (current, total, message)

        Returns:
            事件列表，按 name 排序
        """
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

        # Phase 1: 分页收集所有基本事件信息
        basic_items = []
        marker = None
        page_num = 0

        while True:
            page_num += 1
            body = {
                "meta_detail": {
                    "vehicle_name": [plate],
                    "filter_name": ["light_recording", "manual_recording", "hmi_console_event"],
                    "collect_time": [start_time, end_time],
                },
                "page_size": 50,
            }
            if marker:
                body["marker"] = marker

            logger.info(f"Fetching page {page_num}, current total: {len(basic_items)}")
            if progress_callback:
                progress_callback(
                    len(basic_items), None,
                    f"正在获取事件列表 (第 {page_num} 页)..."
                )

            resp = requests.post(
                f"{self.base_url}/api/v1/event/_search",
                headers=headers,
                json=body,
                timeout=settings.ESS_API_TIMEOUT
            )
            resp.raise_for_status()
            data = resp.json()

            basic_items.extend(data.get("content", []))
            marker = data.get("marker")
            if not marker:
                break

        logger.info(f"Total events found: {len(basic_items)}")
        if progress_callback:
            progress_callback(
                len(basic_items), len(basic_items),
                f"共找到 {len(basic_items)} 个事件，开始拉取详情..."
            )

        # Phase 2: 并发拉取详情
        def fetch_detail(item):
            event_id = item.get("id", "")
            name = item.get("name", "")
            package_version = item.get("fmp_task_info", {}).get("package_version", "")
            # 直接从列表数据中获取 description
            desc = item.get("description", "")

            return {
                "event_id": event_id,
                "name": name,
                "description": desc,
                "package_version": package_version,
                "ess_url": f"{self.base_url}/events/detail?id={event_id}",
                "mviz_url": f"https://mviz.momenta.works/player/v5/?meta=//{self.base_url.replace('https://', '')}/open-ui/v1/event/{event_id}/mviz-meta",
            }

        events = []
        completed = 0

        with ThreadPoolExecutor(max_workers=settings.ESS_CONCURRENT_WORKERS) as executor:
            futures = [executor.submit(fetch_detail, item) for item in basic_items]
            for future in futures:
                result = future.result()
                events.append(result)
                completed += 1
                if progress_callback and completed % 5 == 0:  # 每 5 个更新一次
                    progress_callback(
                        completed, len(basic_items),
                        f"正在拉取详情 ({completed}/{len(basic_items)})..."
                    )

        events.sort(key=lambda e: e["name"])
        logger.info(f"All {len(events)} events fetched with details")
        return events

    def create_share_key(
        self,
        plate: str,
        start_time: str,
        end_time: str
    ) -> str:
        """
        创建 ESS shareKey 返回搜索结果链接

        Args:
            plate: 车牌号
            start_time: 开始时间
            end_time: 结束时间

        Returns:
            ESS 分享链接，例如 "https://ess.momenta.works/events?shareKey=xxx"
        """
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

        body = {
            "name": f"{plate} {start_time} - {end_time}",
            "content": {
                "meta_detail": {
                    "vehicle_name": [plate],
                    "filter_name": ["light_recording", "manual_recording", "hmi_console_event"],
                    "country": ["CN"],
                    "collect_time": [start_time, end_time],
                }
            }
        }

        resp = requests.post(
            f"{self.base_url}/api/v1/event-query",
            headers=headers,
            json=body,
            timeout=settings.ESS_API_TIMEOUT
        )
        resp.raise_for_status()
        data = resp.json()
        share_key = data.get("data", {}).get("id") or data.get("id")

        if not share_key:
            raise RuntimeError(f"Failed to create shareKey: {data}")

        share_url = f"{self.base_url}/events?shareKey={share_key}"
        logger.info(f"Created share key: {share_url}")
        return share_url

    def get_package_version(
        self,
        plate: str,
        start_time: str,
        end_time: str
    ) -> str:
        """
        获取测试包版本号

        从第一个事件的 fmp_task_info 中提取 package_version

        Args:
            plate: 车牌号
            start_time: 开始时间
            end_time: 结束时间

        Returns:
            版本号字符串，如果获取失败返回空字符串
        """
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json"
        }

        try:
            resp = requests.post(
                f"{self.base_url}/api/v1/event/_search",
                headers=headers,
                json={
                    "meta_detail": {
                        "vehicle_name": [plate],
                        "collect_time": [start_time, end_time],
                    },
                    "page_size": 1,
                },
                timeout=settings.ESS_API_TIMEOUT
            )

            if resp.status_code != 200:
                logger.warning(f"Failed to get package version: {resp.status_code}")
                return ""

            items = resp.json().get("content", [])
            if not items:
                logger.warning("No events found for package version")
                return ""

            version = items[0].get("fmp_task_info", {}).get("package_version", "")
            logger.info(f"Package version: {version or '(empty)'}")
            return version

        except Exception as e:
            logger.warning(f"Error getting package version: {e}")
            return ""
