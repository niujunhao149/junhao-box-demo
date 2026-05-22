"""
FST 树写入服务：从 Scenario Set 或 Event ID 列表导入事件到 FST 叶子节点
"""
import re
import urllib.parse
import requests
from typing import List, Tuple


ESS_BASE = "https://ess.momenta.works"


def _hdrs(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def parse_fst_path(url_or_path: str) -> Tuple[str, str]:
    """
    从 ESS FST URL 解析出 repos 和 inner_path。
    支持格式：
      https://ess.momenta.works/evaluation/fst/AES_FST?branch=master&path=AES_FST%2F...
    返回 (repos, inner_path)，inner_path 不含 repos 前缀。
    """
    # 从 URL 提取 path 参数
    m = re.search(r'[?&]path=([^&]+)', url_or_path)
    if m:
        full_path = urllib.parse.unquote(m.group(1))
    else:
        full_path = urllib.parse.unquote(url_or_path.strip())

    # 提取 repos（第一段）和 inner_path（其余部分）
    parts = full_path.split("/", 1)
    if len(parts) == 2:
        return parts[0], parts[1]
    return full_path, ""


def parse_scenario_set_url(url: str) -> Tuple[str, str]:
    """从 ESS Scenario Set URL 提取 repos 和 name（或 id）。"""
    # ?id=xxx&repos=yyy 格式
    m_id   = re.search(r'[?&]id=([^&]+)', url)
    m_name = re.search(r'[?&]name=([^&]+)', url)
    m_repo = re.search(r'[?&]repos=([^&]+)', url)
    ss_id   = urllib.parse.unquote(m_id.group(1))   if m_id   else None
    ss_name = urllib.parse.unquote(m_name.group(1)) if m_name else None
    repos   = urllib.parse.unquote(m_repo.group(1)) if m_repo else "AES_FST"
    return repos, ss_id, ss_name


def get_event_ids_from_scenario_set(repos: str, ss_id: str | None, ss_name: str | None, token: str) -> List[str]:
    """从 Scenario Set 扫出所有 event_id。"""
    hdrs = _hdrs(token)
    # 先查 set info
    body = {}
    if ss_name:
        body = {"name": ss_name, "repos": repos}
    r = requests.post(f"{ESS_BASE}/api/v1/scenario-set/_list", headers=hdrs, json=body, timeout=15)
    r.raise_for_status()
    items = r.json().get("items", [])
    if not items:
        raise ValueError(f"找不到 Scenario Set: repos={repos} name={ss_name}")
    ss = items[0]
    resolved_id = ss_id or ss["id"]

    all_ids, marker = [], None
    while True:
        scan_body = {"id": resolved_id, "repos": repos, "page_size": 100}
        if marker:
            scan_body["marker"] = marker
        r2 = requests.post(f"{ESS_BASE}/api/v1/scenario-set/_scan/scenario",
                           headers=hdrs, json=scan_body, timeout=30)
        r2.raise_for_status()
        d = r2.json()
        batch = d.get("items", [])
        all_ids.extend(x["event_id"] for x in batch)
        marker = d.get("marker")
        if len(batch) < 100 or not marker:
            break
    return all_ids


def parse_event_ids_text(text: str) -> List[str]:
    """从文本中提取 event_id（24位hex）。"""
    return list(dict.fromkeys(
        m.group(0) for m in re.finditer(r'[0-9a-f]{24}', text.lower())
    ))


def get_node_count(repos: str, inner_path: str, token: str) -> int:
    hdrs = _hdrs(token)
    r = requests.get(
        f"{ESS_BASE}/api/v1/tree/graph/{repos}/{urllib.parse.quote(inner_path, safe='')}",
        params={"branch": "master"}, headers=hdrs, timeout=15,
    )
    return r.json().get("count", 0)


def import_to_fst(
    source: str,          # Scenario Set URL 或 Event ID 文本
    fst_url: str,         # FST 叶子节点 URL
    token: str,
) -> dict:
    """
    主入口：解析来源 → 获取 event_ids → 写入 FST 节点。
    返回 {repos, path, event_count, before, after}
    """
    repos, inner_path = parse_fst_path(fst_url)
    if not inner_path:
        raise ValueError("无法解析 FST 路径，请检查 URL")

    hdrs = _hdrs(token)
    before = get_node_count(repos, inner_path, token)

    # 判断来源：Scenario Set URL 还是裸 Event ID 列表
    source = source.strip()
    if "scenario_set" in source or "repos=" in source:
        ss_repos, ss_id, ss_name = parse_scenario_set_url(source)
        event_ids = get_event_ids_from_scenario_set(ss_repos, ss_id, ss_name, token)
    else:
        event_ids = parse_event_ids_text(source)

    if not event_ids:
        raise ValueError("未能解析出任何 Event ID，请检查输入")

    r = requests.post(f"{ESS_BASE}/api/v1/tree/_import/event", headers=hdrs, json={
        "repos": repos, "branch": "master",
        "path": inner_path, "event_ids": event_ids,
    }, timeout=60)
    r.raise_for_status()
    d = r.json()
    if "error" in d.get("message", "").lower():
        raise RuntimeError(d.get("message"))

    after = get_node_count(repos, inner_path, token)
    return {
        "repos": repos,
        "path": inner_path,
        "event_count": len(event_ids),
        "added": after - before,
        "before": before,
        "after": after,
        "version": d.get("version", ""),
    }
