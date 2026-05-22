#!/usr/bin/env python3
"""
数据集对比与运算脚本
对比多个 Event Set / Scenario Set 的数据重叠，执行集合运算，支持创建新 Scenario Set

输入: stdin JSON
输出: stdout PROGRESS:pct:msg 和 RESULT:{json}
"""

import sys
import os
from pathlib import Path

# Force unbuffered stdout
sys.stdout.reconfigure(line_buffering=True)

# ============================================================================
# 虚拟环境自举 - 云端 Docker 直接有 pyevents，本地需要 venv
# ============================================================================
try:
    import pyevents  # noqa: F401
    _HAS_PYEVENTS = True
except ImportError:
    _HAS_PYEVENTS = False

if not _HAS_PYEVENTS:
    SKILL_DIR = Path("C:/Users/junhao.niu/.claude/skills/simict-task-creator")
    VENV_DIR = SKILL_DIR / ".venv"
    MOMENTA_INDEX = "https://artifactory.momenta.works/artifactory/api/pypi/pypi-momenta/simple"
    PL_INDEX = "https://artifactory.momenta.works/artifactory/api/pypi/pypi-pl/simple"

    def _in_venv():
        return hasattr(sys, "prefix") and Path(sys.prefix).resolve() == VENV_DIR.resolve()

    def _venv_is_ready():
        venv_python = VENV_DIR / "Scripts" / "python.exe"
        return venv_python.is_file() and (VENV_DIR / ".installed").is_file()

    def _create_venv_and_install():
        import subprocess
        import shutil
        if VENV_DIR.exists():
            shutil.rmtree(VENV_DIR)
        print("首次运行，创建虚拟环境...")
        result = subprocess.run(
            [sys.executable, "-m", "venv", "--prompt", "dataset-compare", str(VENV_DIR)],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"创建虚拟环境失败: {result.stderr}")
            sys.exit(1)
        venv_python = str(VENV_DIR / "Scripts" / "python.exe")
        subprocess.run([venv_python, "-m", "pip", "install", "-q", "--upgrade", "pip"], capture_output=True)
        print("安装依赖...")
        result = subprocess.run(
            [venv_python, "-m", "pip", "install", "-q", "pyevents", "pyfeishu",
             "--index-url", MOMENTA_INDEX, "--extra-index-url", PL_INDEX,
             "--extra-index-url", "https://pypi.org/simple/"],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            print(f"安装依赖失败: {result.stderr}")
            sys.exit(1)
        subprocess.run([venv_python, "-m", "pip", "install", "-q", "pydantic>=1.10.18,<2"], capture_output=True, text=True)
        (VENV_DIR / ".installed").write_text("1.0\n")
        print("虚拟环境创建完成")

    def _bootstrap_venv():
        if _in_venv():
            return
        if not _venv_is_ready():
            _create_venv_and_install()
        venv_python = str(VENV_DIR / "Scripts" / "python.exe")
        os.execv(venv_python, [venv_python] + sys.argv)

    _bootstrap_venv()
# ============================================================================

import json
import re
import time
from typing import Dict, List, Optional, Set, Tuple
from collections import Counter
from urllib.parse import urlparse, parse_qs, quote as url_quote

import requests
requests.packages.urllib3.disable_warnings()
os.environ["CURL_CA_BUNDLE"] = ""
os.environ["REQUESTS_CA_BUNDLE"] = ""

from pyevents.event_library_services import (
    get_scenario_set_event_ids,
    get_scenario_set_details,
    search_events_new_v2,
    import_scenario_set_from_events,
)
from pyevents.utils.enums import EventSearchReturnMetaV2

# ── Keycloak 认证 ──────────────────────────────────────────────────
KEYCLOAK_URL = "https://keycloak-prod-cla.mmtwork.com/auth"
REALM_NAME = "momenta-prod"
CLIENT_ID = "ess-client"
TOKEN_URL = f"{KEYCLOAK_URL}/realms/{REALM_NAME}/protocol/openid-connect/token"
KC_USER = os.environ.get("KEYCLOAK_USERNAME", "cp_system_gpt")
KC_PASS = os.environ.get("KEYCLOAK_PASSWORD", "csg_2025")

_token_cache = None
_token_expiry = 0


def _get_keycloak_token() -> str:
    global _token_cache, _token_expiry
    if _token_cache and time.time() < _token_expiry:
        return _token_cache
    data = {
        "client_id": CLIENT_ID,
        "username": KC_USER,
        "password": KC_PASS,
        "grant_type": "password",
    }
    resp = requests.post(TOKEN_URL, data=data, timeout=10)
    resp.raise_for_status()
    tokens = resp.json()
    _token_cache = tokens["access_token"]
    _token_expiry = time.time() + tokens.get("expires_in", 300) - 60
    return _token_cache


def _ess_headers() -> dict:
    return {"Authorization": f"Bearer {_get_keycloak_token()}", "Content-Type": "application/json"}


# ── ESS REST API: 获取 Event Set 的所有 Event ID ────────────────────
ESS_BASE = "https://ess.momenta.works"


def fetch_event_set_ids(event_set_id: str) -> List[str]:
    """通过 ESS REST API 分页获取 Event Set 中的所有 Event ID"""
    all_ids = []
    marker = ""
    page_size = 200
    first_page = True

    while True:
        body = {
            "event_set_id": event_set_id,
            "page_size": page_size,
        }
        if marker:
            body["marker"] = marker

        resp = requests.post(
            f"{ESS_BASE}/api/v1/event/_search",
            json=body,
            headers=_ess_headers(),
            timeout=30,
            verify=False,
        )
        resp.raise_for_status()
        data = resp.json()
        events = data.get("content", [])

        if not events:
            break

        # 验证第一页返回的事件确实属于该 event_set
        # ESS API 在 event_set_id 无效时会忽略过滤条件返回全部事件
        if first_page:
            first_page = False
            sample_sets = events[0].get("event_sets", [])
            if event_set_id not in sample_sets and not any(
                es.get("id", es) == event_set_id if isinstance(es, dict) else es == event_set_id
                for es in sample_sets
            ):
                # 返回的事件不属于该 event_set，说明 ID 无效
                return []

        for e in events:
            eid = e.get("id", "")
            if eid:
                all_ids.append(eid)

        marker = data.get("marker", "")
        if not marker:
            break

    return all_ids


def fetch_event_set_info(event_set_id: str) -> Optional[dict]:
    """获取 Event Set 基本信息（名称、事件数等）"""
    try:
        resp = requests.get(
            f"{ESS_BASE}/api/v1/event-set/{event_set_id}",
            headers=_ess_headers(),
            timeout=10,
            verify=False,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


def fetch_scenario_set_ids(scenario_set_id: str = None, repos: str = None, name: str = None) -> List[str]:
    """通过 ESS REST API 获取 Scenario Set 中的所有 Event ID
    支持两种方式：
    1. scenario_set_id（24位hex）
    2. repos + name
    """
    all_event_ids = []
    page = 0
    page_size = 100

    # 构造请求体
    if scenario_set_id:
        # 先获取 repos
        try:
            resp = requests.get(
                f"{ESS_BASE}/api/v1/scenario-set/_id/{scenario_set_id}",
                headers=_ess_headers(), timeout=30, verify=False,
            )
            if resp.status_code != 200:
                return []
            detail = resp.json()
            repos = detail.get("repos", "")
            if not repos:
                return []
        except Exception:
            return []
        base_body = {"repos": repos, "id": scenario_set_id}
    elif repos and name:
        base_body = {"repos": repos, "name": name}
    else:
        return []

    # 分页获取所有 scenarios
    while True:
        try:
            body = {**base_body, "size": page_size, "page": page}
            r = requests.post(
                f"{ESS_BASE}/api/v1/scenario-set/_list/scenario",
                json=body, headers=_ess_headers(), timeout=60, verify=False,
            )
            r.raise_for_status()
            data = r.json()
            items = data.get("items", []) if isinstance(data, dict) else data
            total = data.get("total", len(items)) if isinstance(data, dict) else len(items)

            for item in items:
                eid = item.get("event_id", "")
                if eid:
                    all_event_ids.append(eid)

            if len(all_event_ids) >= total or not items:
                break
            page += 1
        except Exception:
            break

    return list(set(all_event_ids))


def fetch_fst_tree_event_ids(repos: str, inner_path: str) -> List[str]:
    """通过 /tree/graph 找叶子节点，再用 /tree/_scan/scenario 分页获取所有 event_id"""
    path_encoded = url_quote(inner_path, safe='/')
    try:
        resp = requests.get(
            f"{ESS_BASE}/api/v1/tree/graph/{repos}/{path_encoded}?branch=master",
            headers=_ess_headers(), timeout=30, verify=False,
        )
        resp.raise_for_status()
        graph = resp.json()
    except Exception as e:
        progress(0, f"FST tree graph 获取失败: {e}")
        return []

    leaves: List[dict] = []

    def collect_leaves(node):
        if node.get("leaf"):
            leaves.append({"name": node["name"], "path": node["path"]})
        for child in node.get("children", []):
            collect_leaves(child)

    collect_leaves(graph)

    all_ids: set = set()
    for leaf in leaves:
        leaf_path = leaf["path"]
        leaf_name = leaf["name"]
        try:
            marker = ""
            leaf_ids = []
            while True:
                body = {"repos": repos, "type": "branch", "version": "master",
                        "path": leaf_path, "limit": 200}
                if marker:
                    body["marker"] = marker
                r = requests.post(
                    f"{ESS_BASE}/api/v1/tree/_scan/scenario",
                    json=body, headers=_ess_headers(), timeout=60, verify=False,
                )
                r.raise_for_status()
                data = r.json()
                items = data.get("items") or []
                for item in items:
                    eid = item.get("event_id", "")
                    if eid:
                        leaf_ids.append(eid)
                marker = data.get("marker", "")
                if not items or not marker:
                    break
            all_ids.update(leaf_ids)
            progress(0, f"  FST 叶节点 '{leaf_name}': {len(leaf_ids)} 条")
        except Exception as e:
            progress(0, f"  FST 叶节点获取失败 '{leaf_name}': {e}")

    return list(all_ids)


# ── 进度输出 ────────────────────────────────────────────────────────
def progress(pct: int, msg: str):
    print(f"PROGRESS:{pct}:{msg}")


# ── 输入解析 ────────────────────────────────────────────────────────
def parse_input_line(line: str) -> Tuple[str, str]:
    """解析单行输入 → (type, id_or_url_info)
    支持:
    1. Event Set ID: 69cd3f21283d487b9ba95a70
    2. Event Set URL: https://ess.momenta.works/events/set/detail?id=69cd3f21283d487b9ba95a70
    3. Scenario Set URL: https://ess.momenta.works/evaluation/scenario_set/detail?repos=ADAS_FST&name=...
    4. Scenario Set link (mviz): https://mviz.momenta.works/.../scenario-set/xxx/yyy
    5. ESS shareKey URL: https://ess.momenta.works/events?shareKey=69cf3ed9888a226dfb63b021
    """
    line = line.strip()
    if not line:
        return ("unknown", "")

    # ESS shareKey URL: /events?shareKey=xxx
    m = re.search(r'[?&]shareKey=([a-f0-9]{24})', line, re.IGNORECASE)
    if m:
        return ("share_key", m.group(1))

    # Event Set URL: /events/set/detail?id=xxx
    m = re.search(r'/events/set/detail\?id=([a-f0-9]+)', line, re.IGNORECASE)
    if m:
        return ("event_set", m.group(1))

    # Scenario Set URL (ESS): ?repos=XXX&name=YYY
    if "scenario_set/detail" in line or "scenario-set/detail" in line:
        return ("scenario_set", line)

    # Scenario Set link (mviz): /scenario-set/xxx/yyy
    m = re.search(r'/scenario-set/([a-f0-9]+)/([a-f0-9]+)', line, re.IGNORECASE)
    if m:
        return ("scenario_set_id", m.group(2))

    # Mviz event link: /event/xxx/
    m = re.search(r'/event/([a-f0-9]{24})/', line, re.IGNORECASE)
    if m:
        return ("event", m.group(1))

    # ESS event detail URL: ?id=xxx
    m = re.search(r'[?&]id=([a-f0-9]{24})', line, re.IGNORECASE)
    if m:
        return ("event", m.group(1))

    # Bare 24-char hex ID — 需要自动探测类型
    if re.match(r'^[a-f0-9]{24}$', line, re.IGNORECASE):
        return ("auto_detect_id", line)

    # FST node URL: /evaluation/fst/{repos}?path=...
    m = re.search(r'/evaluation/fst/([^/?]+)', line, re.IGNORECASE)
    if m:
        repos = m.group(1)
        fst_params = parse_qs(urlparse(line).query)
        path = fst_params.get("path", [None])[0]
        if path:
            # path = "AES_FST/攻坚FST评测集/..." → 去掉 repos 前缀得到 inner_path
            parts = path.rstrip("/").split("/")
            inner_path = "/".join(parts[1:]) if parts[0] == repos else "/".join(parts)
            return ("fst_node", f"{repos}|{inner_path}")

    return ("unknown", line)


def resolve_scenario_set_url(url: str) -> Tuple[str, str]:
    """从 Scenario Set URL 提取 id 或 repos+name"""
    parsed = urlparse(url)
    params = parse_qs(parsed.query)
    # 优先用 id 直接解析（避免 repos+name 查询失败）
    id_val = params.get("id", [None])[0]
    if id_val and re.match(r'^[a-f0-9]{24}$', id_val, re.IGNORECASE):
        return ("scenario_set_id", id_val)
    # 降级用 repos+name
    repos = params.get("repos", [None])[0]
    name = params.get("name", [None])[0]
    if repos and name:
        return ("scenario_set_repos_name", f"{repos}|{name}")
    return ("unknown", url)


def fetch_share_key_events(share_key: str) -> Tuple[List[str], str]:
    """通过 shareKey 获取 EQL 查询并用 EQL 搜索事件，返回 (event_ids, description)"""
    try:
        resp = requests.get(
            f"{ESS_BASE}/ui/v1/event-query/{share_key}",
            headers=_ess_headers(), timeout=10, verify=False,
        )
        if resp.status_code != 200:
            return ([], f"shareKey 查询失败: HTTP {resp.status_code}")

        data = resp.json()
        eql = data.get("eql", "")
        if not eql:
            return ([], "shareKey 未返回有效 EQL 查询")

        # 用 EQL 搜索事件
        all_ids = []
        marker = ""
        page_size = 200

        while True:
            body = {"eql": eql, "page_size": page_size}
            if marker:
                body["marker"] = marker

            r = requests.post(
                f"{ESS_BASE}/api/v1/event/_search",
                json=body, headers=_ess_headers(), timeout=30, verify=False,
            )
            r.raise_for_status()
            result = r.json()
            events = result.get("content", [])

            if not events:
                break

            for e in events:
                eid = e.get("id", "")
                if eid:
                    all_ids.append(eid)

            marker = result.get("marker", "")
            if not marker:
                break

        return (all_ids, f"EQL: {eql[:60]}...")
    except Exception as e:
        return ([], f"shareKey 解析失败: {e}")


def auto_detect_id_type(id_str: str) -> Tuple[str, str]:
    """自动探测 24 位 hex ID 的真实类型：share_key / event_set / scenario_set / event"""
    # 1. 先试 shareKey（/ui/v1/event-query/{id}）
    try:
        resp = requests.get(
            f"{ESS_BASE}/ui/v1/event-query/{id_str}",
            headers=_ess_headers(), timeout=10, verify=False,
        )
        if resp.status_code == 200 and resp.json().get("eql"):
            return ("share_key", id_str)
    except Exception:
        pass

    # 2. 再试 event-set API
    try:
        resp = requests.get(
            f"{ESS_BASE}/api/v1/event-set/{id_str}",
            headers=_ess_headers(), timeout=10, verify=False,
        )
        if resp.status_code == 200:
            return ("event_set", id_str)
    except Exception:
        pass

    # 3. 再试 scenario-set（REST API）
    try:
        resp = requests.get(
            f"{ESS_BASE}/api/v1/scenario-set/_id/{id_str}",
            headers=_ess_headers(), timeout=10, verify=False,
        )
        if resp.status_code == 200 and resp.json():
            return ("scenario_set_id", id_str)
    except Exception:
        pass

    # 4. 最后试 event
    try:
        resp = requests.get(
            f"{ESS_BASE}/api/v1/event/{id_str}",
            headers=_ess_headers(), timeout=10, verify=False,
        )
        if resp.status_code == 200:
            return ("event", id_str)
    except Exception:
        pass

    # 无法识别，返回 unknown 让上层提示用户
    return ("unknown", id_str)


def resolve_input_set(content: str, label: str) -> Dict:
    """解析一组输入 → {label, event_ids: set, source_details: list}"""
    lines = [l.strip() for l in content.strip().splitlines() if l.strip()]
    event_ids = set()
    source_details = []

    for line in lines:
        dtype, dval = parse_input_line(line)

        if dtype == "unknown":
            progress(0, f"[{label}] 无法识别: {line[:50]}...")
            continue

        if dtype == "event":
            event_ids.add(dval)
            source_details.append({"type": "event", "id": dval})
            continue

        if dtype == "share_key":
            progress(0, f"[{label}] 正在解析 ESS 分享链接: {dval[:16]}...")
            ids, desc = fetch_share_key_events(dval)
            event_ids.update(ids)
            source_details.append({
                "type": "share_key", "id": dval,
                "description": desc, "count": len(ids),
            })
            progress(0, f"[{label}] 分享链接获取到 {len(ids)} 条事件（{desc}）")
            continue

        if dtype == "auto_detect_id":
            progress(0, f"[{label}] 正在自动识别 ID 类型: {dval[:16]}...")
            detected_type, detected_val = auto_detect_id_type(dval)
            progress(0, f"[{label}] 识别为: {detected_type}")
            # 重新当作已识别类型处理
            dtype, dval = detected_type, detected_val
            if dtype == "unknown":
                progress(0, f"[{label}] 无法识别 ID: {dval}（不是 Event Set / Scenario Set / Event）")
                continue
            # fall through to matching handler below

        if dtype == "event_set":
            # 先获取 event set 信息
            info = fetch_event_set_info(dval)
            set_name = info.get("name", dval[:16]) if info else dval[:16]
            total = info.get("event_count", "?") if info else "?"

            progress(0, f"[{label}] 正在从 Event Set 获取事件: {set_name}（共 {total} 条）...")
            try:
                ids = fetch_event_set_ids(dval)
                event_ids.update(ids)
                source_details.append({
                    "type": "event_set", "id": dval,
                    "name": set_name, "count": len(ids),
                })
                progress(0, f"[{label}] Event Set '{set_name}': 获取到 {len(ids)} 条事件")
            except Exception as e:
                progress(0, f"[{label}] Event Set 获取失败: {e}")
            continue

        if dtype == "scenario_set":
            subtype, subval = resolve_scenario_set_url(dval)
            if subtype == "scenario_set_id":
                progress(0, f"[{label}] 正在从 Scenario Set 获取事件: {subval[:16]}...")
                try:
                    ids = fetch_scenario_set_ids(subval)
                    event_ids.update(ids)
                    source_details.append({
                        "type": "scenario_set", "id": subval, "count": len(ids),
                    })
                    progress(0, f"[{label}] Scenario Set 获取到 {len(ids)} 条事件")
                except Exception as e:
                    progress(0, f"[{label}] Scenario Set 获取失败: {e}")
            elif subtype == "scenario_set_repos_name":
                repos, name = subval.split("|", 1)
                progress(0, f"[{label}] 正在从 Scenario Set 获取事件: {name[:40]}...")
                try:
                    ids = fetch_scenario_set_ids(repos=repos, name=name)
                    event_ids.update(ids)
                    source_details.append({
                        "type": "scenario_set", "repos": repos,
                        "name": name, "count": len(ids),
                    })
                    progress(0, f"[{label}] Scenario Set 获取到 {len(ids)} 条事件")
                except Exception as e:
                    progress(0, f"[{label}] Scenario Set 获取失败: {e}")
            continue

        if dtype == "scenario_set_id":
            progress(0, f"[{label}] 正在从 Scenario Set 获取事件: {dval[:16]}...")
            try:
                ids = fetch_scenario_set_ids(dval)
                event_ids.update(ids)
                source_details.append({
                    "type": "scenario_set", "id": dval, "count": len(ids),
                })
            except Exception as e:
                progress(0, f"[{label}] Scenario Set 获取失败: {e}")
            continue

        if dtype == "fst_node":
            repos, inner_path = dval.split("|", 1)
            node_name = inner_path.rstrip("/").split("/")[-1]
            progress(0, f"[{label}] 正在递归遍历 FST 节点: {node_name}...")
            try:
                ids = fetch_fst_tree_event_ids(repos=repos, inner_path=inner_path)
                event_ids.update(ids)
                source_details.append({
                    "type": "fst_node", "repos": repos,
                    "path": inner_path, "name": node_name, "count": len(ids),
                })
                progress(0, f"[{label}] FST 节点共获取到 {len(ids)} 条事件")
            except Exception as e:
                progress(0, f"[{label}] FST 节点获取失败: {e}")
            continue

    return {
        "label": label,
        "event_ids": event_ids,
        "source_details": source_details,
    }


# ── 集合运算 ────────────────────────────────────────────────────────
def compute_set_operations(sets_data: List[Dict], operations: List[str], difference_source: str) -> Dict:
    result = {"sets": [], "pairwise": [], "operations": {}}

    for s in sets_data:
        result["sets"].append({"label": s["label"], "count": len(s["event_ids"])})

    for i in range(len(sets_data)):
        for j in range(i + 1, len(sets_data)):
            a, b = sets_data[i], sets_data[j]
            intersection = a["event_ids"] & b["event_ids"]
            union = a["event_ids"] | b["event_ids"]
            result["pairwise"].append({
                "set_a": a["label"], "set_b": b["label"],
                "intersection_count": len(intersection),
                "union_count": len(union),
                "only_a_count": len(a["event_ids"] - b["event_ids"]),
                "only_b_count": len(b["event_ids"] - a["event_ids"]),
                "jaccard": round(len(intersection) / len(union), 4) if union else 1.0,
            })

    if "union" in operations:
        all_ids = set()
        for s in sets_data:
            all_ids |= s["event_ids"]
        result["operations"]["union"] = {
            "count": len(all_ids), "event_ids": sorted(all_ids),
            "label": " ∪ ".join(s["label"] for s in sets_data),
        }

    if "intersection" in operations:
        common = sets_data[0]["event_ids"].copy()
        for s in sets_data[1:]:
            common &= s["event_ids"]
        result["operations"]["intersection"] = {
            "count": len(common), "event_ids": sorted(common),
            "label": " ∩ ".join(s["label"] for s in sets_data),
        }

    if "difference" in operations:
        source_set = None
        other_sets = []
        for s in sets_data:
            if s["label"] == difference_source:
                source_set = s
            else:
                other_sets.append(s)
        if source_set:
            diff = source_set["event_ids"].copy()
            for s in other_sets:
                diff -= s["event_ids"]
            other_labels = " − ".join(s["label"] for s in other_sets)
            result["operations"]["difference"] = {
                "count": len(diff), "event_ids": sorted(diff),
                "label": f"{source_set['label']} − {other_labels}",
            }

    if "symmetric_difference" in operations:
        if len(sets_data) == 2:
            sym_diff = sets_data[0]["event_ids"] ^ sets_data[1]["event_ids"]
        else:
            counter = Counter()
            for s in sets_data:
                counter.update(s["event_ids"])
            sym_diff = {eid for eid, cnt in counter.items() if cnt == 1}
        result["operations"]["symmetric_difference"] = {
            "count": len(sym_diff), "event_ids": sorted(sym_diff),
            "label": " △ ".join(s["label"] for s in sets_data),
        }

    return result


# ── 元数据获取 ──────────────────────────────────────────────────────
def fetch_metadata_distributions(sets_data: List[Dict]) -> Dict:
    all_ids = set()
    for s in sets_data:
        all_ids |= s["event_ids"]
    if not all_ids:
        return {"vehicles": {}, "versions": {}, "routes": {}}

    id_list = sorted(all_ids)
    meta_fields = [
        EventSearchReturnMetaV2.EVENT_ID,
        EventSearchReturnMetaV2.VEHICLE_NAME,
        EventSearchReturnMetaV2.PACKAGE_VERSION,
        EventSearchReturnMetaV2.ROUTE_NAME,
        EventSearchReturnMetaV2.EVENT_NAME,
        EventSearchReturnMetaV2.COLLECT_TIME,
    ]

    events = []
    for i in range(0, len(id_list), 1000):
        batch = id_list[i:i + 1000]
        try:
            batch_events = search_events_new_v2(ids=batch, return_meta=meta_fields)
            if batch_events:
                events.extend(batch_events)
        except Exception:
            pass

    event_meta = {}
    for e in events:
        eid = e.get("event_id", e.get("id", ""))
        event_meta[eid] = e

    result = {"vehicles": {}, "versions": {}, "routes": {}, "event_details": {}}
    for s in sets_data:
        label = s["label"]
        vehicles = Counter()
        versions = Counter()
        routes = Counter()
        details = []
        for eid in s["event_ids"]:
            meta = event_meta.get(eid, {})
            vehicles[meta.get("vehicle_name", "未知")] += 1
            versions[meta.get("package_version", "未知")] += 1
            routes[meta.get("route_name", "未知")] += 1
            details.append({
                "event_id": eid,
                "event_name": meta.get("event_name", ""),
                "vehicle_name": meta.get("vehicle_name", ""),
                "package_version": meta.get("package_version", ""),
                "route_name": meta.get("route_name", ""),
                "collect_time": meta.get("collect_time", ""),
            })
        result["vehicles"][label] = dict(vehicles.most_common(20))
        result["versions"][label] = dict(versions.most_common(20))
        result["routes"][label] = dict(routes.most_common(20))
        result["event_details"][label] = details

    return result


def create_scenario_set(event_ids: List[str], name: str, repos: str, owners: List[str] = None) -> Dict:
    try:
        headers = _ess_headers()
        chunks = [event_ids[i:i + 1000] for i in range(0, len(event_ids), 1000)]
        for chunk in chunks:
            body = {"repos": repos, "name": name, "event_ids": chunk}
            if owners:
                body["owners"] = owners
            resp = requests.post(
                f"{ESS_BASE}/api/v1/scenario-set/_import/event",
                json=body, headers=headers, timeout=120, verify=False,
            )
            resp.raise_for_status()

        # 获取刚创建的 set ID
        r = requests.get(
            f"{ESS_BASE}/api/v1/scenario-set/{repos}/{url_quote(name, safe='')}",
            headers=headers, timeout=30, verify=False,
        )
        set_id = r.json().get("id", "") if r.status_code == 200 else ""

        ess_link = f"https://ess.momenta.works/evaluation/scenario_set/detail?repos={repos}&name={url_quote(name, safe='')}"
        mviz_link = f"https://mviz.momenta.works/player/v5/scenario-set/{set_id}" if set_id else ""
        return {
            "id": set_id, "name": name, "event_count": len(event_ids),
            "ess_link": ess_link, "mviz_link": mviz_link,
        }
    except Exception as e:
        return {"error": str(e)}


# ── 主流程 ──────────────────────────────────────────────────────────
def main():
    input_data = json.loads(sys.stdin.read())

    input_sets = input_data.get("input_sets", [])
    operations = input_data.get("operations", ["compare"])
    difference_source = input_data.get("difference_source", "A")
    create_set = input_data.get("create_scenario_set", False)
    set_name = input_data.get("scenario_set_name", "")
    set_operation = input_data.get("scenario_set_operation", "union")
    repos = input_data.get("repos", "CP_FST")
    owners = input_data.get("owners", [])
    fetch_metadata = input_data.get("fetch_metadata", True)

    total_sets = len(input_sets)

    # Phase 1: Keycloak 认证
    progress(3, "正在进行 Keycloak 认证...")
    try:
        _get_keycloak_token()
        progress(5, "认证成功")
    except Exception as e:
        print("RESULT:" + json.dumps({"error": f"Keycloak 认证失败: {e}"}, ensure_ascii=False))
        return

    # Phase 2: 解析输入数据集
    progress(8, "正在解析输入数据集...")
    sets_data = []
    for i, inp in enumerate(input_sets):
        label = inp.get("label", chr(65 + i))
        content = inp.get("content", "")
        pct = 8 + int((i + 1) / total_sets * 40)
        progress(pct, f"正在解析集合 {label} ({i+1}/{total_sets})...")
        resolved = resolve_input_set(content, label)
        sets_data.append(resolved)

    if not sets_data or all(len(s["event_ids"]) == 0 for s in sets_data):
        print("RESULT:" + json.dumps({"error": "未能解析到任何有效事件，请检查输入"}, ensure_ascii=False))
        return

    # Phase 3: 集合运算
    progress(50, "正在计算集合运算...")
    compare_result = compute_set_operations(sets_data, operations, difference_source)

    # Phase 4: 元数据
    metadata = {}
    if fetch_metadata:
        progress(60, "正在获取事件元数据（车辆/版本/路线）...")
        metadata = fetch_metadata_distributions(sets_data)
        progress(85, "元数据获取完成")

    # Phase 5: 创建 Scenario Set
    created_set = None
    if create_set and set_name:
        progress(90, f"正在创建 Scenario Set: {set_name}...")
        target_ids = compare_result.get("operations", {}).get(set_operation, {}).get("event_ids", [])
        if target_ids:
            created_set = create_scenario_set(target_ids, set_name, repos, owners)
        else:
            created_set = {"error": f"运算 '{set_operation}' 没有结果可创建"}

    progress(100, "对比完成！")

    final_result = {
        "sets": compare_result["sets"],
        "pairwise": compare_result["pairwise"],
        "operations": {},
        "metadata": metadata,
        "created_set": created_set,
    }

    for op_key, op_val in compare_result.get("operations", {}).items():
        final_result["operations"][op_key] = {
            "count": op_val["count"],
            "label": op_val["label"],
            "event_ids": op_val["event_ids"],
        }

    print("RESULT:" + json.dumps(final_result, ensure_ascii=False))


if __name__ == "__main__":
    main()
