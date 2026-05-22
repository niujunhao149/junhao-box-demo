"""
Scenario Set Creator 服务
直接调用 ESS REST API（Keycloak 认证），不依赖本地 skill 路径
"""
import re
import time
import requests
import urllib.parse
from typing import Callable, List, Optional

requests.packages.urllib3.disable_warnings()

ESS_BASE = "https://ess.momenta.works"
KEYCLOAK_TOKEN_URL = "https://keycloak-prod-cla.mmtwork.com/auth/realms/momenta-prod/protocol/openid-connect/token"
KC_CLIENT_ID = "ess-client"

import os
KC_USER = os.environ.get("KEYCLOAK_USERNAME", "cp_system_gpt")
KC_PASS = os.environ.get("KEYCLOAK_PASSWORD", "csg_2025")

_token_cache = None
_token_expiry = 0


def _get_token() -> str:
    global _token_cache, _token_expiry
    if _token_cache and time.time() < _token_expiry:
        return _token_cache
    resp = requests.post(KEYCLOAK_TOKEN_URL, data={
        "client_id": KC_CLIENT_ID,
        "username": KC_USER,
        "password": KC_PASS,
        "grant_type": "password",
    }, timeout=10)
    resp.raise_for_status()
    tokens = resp.json()
    _token_cache = tokens["access_token"]
    _token_expiry = time.time() + tokens.get("expires_in", 300) - 60
    return _token_cache


def _headers() -> dict:
    return {"Authorization": f"Bearer {_get_token()}", "Content-Type": "application/json"}


def _parse_link(link: str) -> Optional[str]:
    """解析 Mviz/ESS 链接或裸 ID → Event ID"""
    link = link.strip()
    if not link:
        return None

    # 裸 24 位 hex Event ID
    if re.match(r'^[a-f0-9]{24}$', link, re.IGNORECASE):
        return link

    # ESS 详情页: ?id=<event_id>
    m = re.search(r'[?&]id=([a-f0-9]{24})', link, re.IGNORECASE)
    if m:
        return m.group(1)

    # Mviz event: /event/<event_id>/
    m = re.search(r'/event/([a-f0-9]{24})/', link, re.IGNORECASE)
    if m:
        return m.group(1)

    # Mviz bag_md5: ?bag_md5=<md5>
    m = re.search(r'bag_md5=([a-fA-F0-9]+)', link)
    if m:
        md5 = m.group(1)
        try:
            r = requests.post(
                f"{ESS_BASE}/api/v1/event/_search",
                json={"eql": f"cla_md5 IN ('{md5}')"},
                headers=_headers(), timeout=15, verify=False,
            )
            r.raise_for_status()
            events = r.json().get("content", [])
            return events[0]["id"] if events else None
        except Exception:
            return None

    return None


class ScenarioService:
    """
    run() 同步执行（通过 asyncio.to_thread 调用）
    on_progress(progress: int, message: str) 全程回调
    返回 dict: {scenario_set_id, scenario_set_name, event_count, ess_link, mviz_link, failed_links}
    """

    def run(
        self,
        name: str,
        links: List[str],
        repos: str,
        on_progress: Callable,
    ) -> dict:
        on_progress(5, "正在进行 Keycloak 认证...")
        _get_token()
        on_progress(15, "认证成功，开始解析链接...")

        event_ids = []
        failed_links = []
        total = len(links)

        for i, link in enumerate(links):
            pct = 15 + int((i + 1) / max(total, 1) * 50)
            eid = _parse_link(link)
            if eid:
                event_ids.append(eid)
                on_progress(pct, f"解析事件 {i+1}/{total}: {eid[:16]}...")
            else:
                failed_links.append(link)
                on_progress(pct, f"无法解析: {link[:40]}...")

        if not event_ids:
            raise RuntimeError("未能解析出任何有效 Event ID，请检查输入链接")

        on_progress(70, f"共解析 {len(event_ids)} 个事件，正在创建 Scenario Set...")

        # 分批创建（每批 1000）
        chunks = [event_ids[i:i+1000] for i in range(0, len(event_ids), 1000)]
        for chunk in chunks:
            r = requests.post(
                f"{ESS_BASE}/api/v1/scenario-set/_import/event",
                json={"repos": repos, "name": name, "event_ids": chunk},
                headers=_headers(), timeout=180, verify=False,
            )
            r.raise_for_status()

        on_progress(90, "正在获取 Scenario Set 信息...")

        # 获取刚创建的 set ID
        r = requests.get(
            f"{ESS_BASE}/api/v1/scenario-set/{repos}/{urllib.parse.quote(name, safe='')}",
            headers=_headers(), timeout=30, verify=False,
        )
        set_id = r.json().get("id", "") if r.status_code == 200 else ""

        name_enc = urllib.parse.quote(name, safe='')
        ess_link = f"{ESS_BASE}/evaluation/scenario_set/detail?repos={repos}&name={name_enc}"

        on_progress(100, f"完成！已创建 Scenario Set，共 {len(event_ids)} 个事件")

        return {
            "scenario_set_id": set_id,
            "scenario_set_name": name,
            "event_count": len(event_ids),
            "ess_link": ess_link,
            "mviz_link": "",
            "failed_links": failed_links,
        }
