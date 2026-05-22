"""CLA 数据查询服务 - 通过 bag 名查询 Mviz 链接"""
import requests
import threading
from typing import List, Dict
from ..utils.logger import get_logger

logger = get_logger(__name__)

_token_lock = threading.Lock()
_cached_token = {"token": None}

KEYCLOAK_URL = "https://keycloak-prod-cla.mmtwork.com/auth/realms/momenta-prod/protocol/openid-connect/token"
DSS_BASE = "https://data.momenta.works/api/v2"
MVIZ_API_BASE = "https://mviz-api.momenta.works"


def _get_token() -> str:
    with _token_lock:
        if _cached_token["token"]:
            return _cached_token["token"]
        r = requests.post(KEYCLOAK_URL, data={
            "client_id": "cla",
            "grant_type": "password",
            "username": "junhao.niu",
            "password": "MMt@20020102",
        }, timeout=15)
        r.raise_for_status()
        token = r.json()["access_token"]
        _cached_token["token"] = token
        return token


def _refresh_token() -> str:
    with _token_lock:
        _cached_token["token"] = None
    return _get_token()


def _search_bag_md5(token: str, bag_name: str) -> str | None:
    h = {"Authorization": f"Bearer {token}"}
    r = requests.post(f"{DSS_BASE}/search/meta", headers=h, json={
        "query": {"bool": {"must": [{"name": bag_name}]}},
        "sort": "create_time:desc",
        "limit": 1,
    }, timeout=30)
    r.raise_for_status()
    d = r.json()
    data = d.get("data", [])
    if not data:
        return None
    return data[0] if isinstance(data[0], str) else None


def _get_mviz_url(token: str, md5: str) -> str | None:
    h = {"Authorization": f"Bearer {token}"}
    r = requests.get(f"{MVIZ_API_BASE}/api/v1/web/mviz-url/cdi/single",
                     headers=h, params={"md5": md5}, timeout=15)
    r.raise_for_status()
    d = r.json()
    return d.get("data", {}).get("mviz_url")


def query_bags_mviz(bag_names: List[str]) -> List[Dict]:
    """批量查询 bag → Mviz 链接"""
    results = []
    try:
        token = _get_token()
    except Exception as e:
        logger.error(f"CLA token 获取失败: {e}")
        return [{"bag": b, "mviz_url": None, "error": "认证失败"} for b in bag_names]

    for bag in bag_names:
        bag = bag.strip()
        if not bag:
            continue
        try:
            md5 = _search_bag_md5(token, bag)
            if not md5:
                results.append({"bag": bag, "mviz_url": None, "error": "未找到"})
                continue
            mviz_url = _get_mviz_url(token, md5)
            results.append({"bag": bag, "md5": md5, "mviz_url": mviz_url, "error": None})
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 401:
                try:
                    token = _refresh_token()
                    md5 = _search_bag_md5(token, bag)
                    mviz_url = _get_mviz_url(token, md5) if md5 else None
                    results.append({"bag": bag, "md5": md5, "mviz_url": mviz_url, "error": None if md5 else "未找到"})
                except Exception as e2:
                    results.append({"bag": bag, "mviz_url": None, "error": str(e2)})
            else:
                results.append({"bag": bag, "mviz_url": None, "error": str(e)})
        except Exception as e:
            logger.warning(f"查询 {bag} 失败: {e}")
            results.append({"bag": bag, "mviz_url": None, "error": str(e)})

    return results
