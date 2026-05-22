import re
import requests
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Dict, Any

ALP_BASE = "https://alp.momenta.works"
AUTH_URL = "https://data.momenta.works/auth/api/v1/user/login"


def _login(username: str, password: str) -> str:
    r = requests.post(AUTH_URL, data={
        "username": username,
        "password": password,
        "platform": "atp",
    }, timeout=15)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"ALP 登录失败: {d.get('message')}")
    return d["token"]


def _hdrs(token: str) -> Dict[str, str]:
    return {"Authorization": token, "Content-Type": "application/json"}


def parse_task_id(raw: str) -> int:
    """从 URL 或纯数字中提取 task_id。"""
    m = re.search(r"id=(\d+)", raw)
    if m:
        return int(m.group(1))
    raw = raw.strip()
    if raw.isdigit():
        return int(raw)
    raise ValueError(f"无法解析 task ID: {raw!r}")


def get_task_info(task_id: int, token: str) -> Dict[str, Any]:
    r = requests.post(
        f"{ALP_BASE}/atp/api/v1/task/get_task_by_id",
        headers=_hdrs(token),
        json={"task_id": task_id},
        timeout=15,
    )
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"获取 task 信息失败: {d.get('message')}")
    return d["data"]


def _get_all_entity_keys(task_id: int, token: str) -> list:
    keys = []
    page = 1
    while True:
        r = requests.post(
            f"{ALP_BASE}/atp/api/v1/task/query_paginated",
            headers=_hdrs(token),
            json={"task_id": task_id, "page_num": page, "page_size": 100},
            timeout=30,
        )
        items = r.json().get("data", {}).get("res_list", [])
        keys.extend(x["entity_key"] for x in items)
        if len(items) < 100:
            break
        page += 1
    return keys


def _get_entity_category(task_id: int, entity_key: str, token: str) -> str:
    r = requests.post(
        f"{ALP_BASE}/atp/api/v1/task/get_event_result",
        headers=_hdrs(token),
        json={"task_id": task_id, "entity_key": entity_key},
        timeout=20,
    )
    d = r.json()
    if d.get("code") != 0:
        return "fetch_error"
    content = d.get("data", {}).get("content", [])
    for item in content:
        checkers = item.get("sim_checker_result", {}).get("checkers_result", {})
        for name, res in checkers.items():
            if name != "Info":
                return res.get("category", "Unknown")
    return "no_result"


def analyze_task(
    task_id_raw: str,
    username: str,
    password: str,
    progress_cb: Callable[[int, int, str], None],
) -> Dict[str, Any]:
    """
    完整分析流程，通过 progress_cb(current, total, message) 汇报进度。
    返回 {task_info, category_counts, total}。
    """
    progress_cb(0, 100, "正在登录 ALP...")
    token = _login(username, password)

    task_id = parse_task_id(task_id_raw)
    progress_cb(5, 100, "获取 task 基本信息...")
    info = get_task_info(task_id, token)

    progress_cb(10, 100, f"拉取 entity 列表（共 {info.get('total_items', '?')} 条）...")
    keys = _get_all_entity_keys(task_id, token)
    total = len(keys)

    counter = Counter()
    done = 0

    def fetch(ek):
        return ek, _get_entity_category(task_id, ek, token)

    workers = min(30, max(5, total // 20))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch, ek): ek for ek in keys}
        for f in as_completed(futures):
            _, cat = f.result()
            counter[cat] += 1
            done += 1
            if done % max(1, total // 20) == 0 or done == total:
                pct = 10 + int(done / total * 85)
                progress_cb(pct, 100, f"分析中 {done}/{total}...")

    progress_cb(100, 100, "完成")
    return {
        "task_info": info,
        "category_counts": dict(counter.most_common()),
        "total": total,
    }
