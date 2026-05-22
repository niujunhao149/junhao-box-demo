"""
DPI 流水线轮询服务
登录 DPI → 获取 Flyte execution token → 持续扫描节点 outputs → 解析 ALP/Sim/ETP/回灌信息
"""
import base64
import json
import time
import logging
from typing import Optional
import requests

logger = logging.getLogger(__name__)

FLYTE_BASE = "https://tos-flyte-ui.dev.dpi-inner.momenta.works"
DPI_LOGIN  = "https://dpi.dev.momenta.works"
ETP_BASE   = "https://etp-backend.momenta.works"

# ── DPI 认证 ────────────────────────────────────────────────────

def get_dpi_token(username: str, password: str) -> str:
    """
    获取 DPI id_token（纯 requests，无需 Playwright）。
    如果 password 字段传入的是 JWT token（eyJ 开头），直接使用。
    """
    if password and password.startswith("eyJ"):
        return password

    AUTH_URL = "https://account-server.dev.dpi-inner.momenta.works/api/v1/mauth/login"
    REFERER  = "https://account-server.dev.dpi-inner.momenta.works/login/index?redirect_uri=https%3A%2F%2Fdpi.dev.momenta.works%2F"

    s = requests.Session()
    r = s.post(AUTH_URL, data={
        "username": username,
        "password": password,
        "type": "ldap",
        "timeout": "604800",
    }, headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": REFERER,
        "Origin": "https://account-server.dev.dpi-inner.momenta.works",
    }, timeout=15)

    if not r.ok:
        raise RuntimeError(f"DPI 登录失败 ({r.status_code}): {r.text[:200]}")

    d = r.json()
    token = d.get("id_token") or s.cookies.get("id_token", "")
    if not token:
        raise RuntimeError(f"DPI 登录成功但未获取到 id_token: {d}")
    return token


def _session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers["Authorization"] = f"Bearer {token}"
    s.headers["Content-Type"] = "application/json"
    return s


# ── Flyte API ────────────────────────────────────────────────────

def get_execution(token: str, exec_id: str) -> dict:
    s = _session(token)
    r = s.get(f"{FLYTE_BASE}/api/v1/executions/ebm-infra/dev/{exec_id}", timeout=15)
    r.raise_for_status()
    return r.json()


def get_execution_phase(token: str, exec_id: str) -> str:
    """返回执行阶段: RUNNING / SUCCEEDED / FAILED 等"""
    d = get_execution(token, exec_id)
    return d.get("closure", {}).get("phase", "UNKNOWN")


def get_node_data(token: str, exec_id: str, node_id: str) -> dict:
    s = _session(token)
    r = s.get(
        f"{FLYTE_BASE}/api/v1/data/node_executions/ebm-infra/dev/{exec_id}/{node_id}",
        timeout=15,
    )
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    return r.json()


def _dec(b64: str):
    """解码 msgpack base64 编码的节点 output。"""
    import msgpack
    raw = base64.b64decode(b64)
    return msgpack.unpackb(raw, raw=False)


def parse_dn5_outputs(token: str, exec_id: str) -> dict:
    """
    解析 n1-0-dn5 的所有 output，返回：
    {
      alp_task_id, alp_task_url, alp_status,
      alp_pos_set_name, alp_pos_set_id,
      sim_task_id, sim_status,
      sim_set_name, sim_set_id,
      etp_url, etp_batch_name, etp_tasktype,
    }
    """
    data = get_node_data(token, exec_id, "n1-0-dn5")
    if not data:
        return {}

    result = {}
    # API 返回 snake_case 的 full_outputs，不是 camelCase 的 fullOutputs
    literals = data.get("full_outputs", data.get("fullOutputs", {})).get("literals", {})
    for key, val in literals.items():
        for idx, item in val.get("map", {}).get("literals", {}).items():
            b64 = item.get("scalar", {}).get("binary", {}).get("value", "")
            if not b64:
                continue
            try:
                d = _dec(b64)
                # data 可能直接在顶层，也可能在 data_task_ctx 里
                ctx = d.get("data_task_ctx") or d
                # idx=1: ALP task summary
                if "alp_task_summary" in ctx:
                    tasks = ctx["alp_task_summary"].get("completed_tasks", [])
                    if tasks:
                        t = tasks[0]
                        result["alp_task_id"]  = t.get("task_id")
                        result["alp_task_url"] = t.get("task_url", "")
                        result["alp_status"]   = t.get("status", "")
                # idx=2: pos/neg sets after ALP
                if "pos_set_name" in ctx:
                    result["alp_pos_set_name"] = ctx.get("pos_set_name", "")
                    result["alp_pos_set_id"]   = ctx.get("pos_set_id", "")
                    result["alp_error_cases"]  = ctx.get("error_case", [])
                # idx=3: Sim task
                if "task_status" in ctx and "alp_task_ids" not in ctx:
                    task_ids = list(ctx.get("task_status", {}).keys())
                    if task_ids:
                        result["sim_task_id"]  = task_ids[0]
                        result["sim_status"]   = ctx["task_status"].get(task_ids[0], "")
                # idx=5: Sim result set (with prefix name)
                if "scenario_set_name" in ctx and "etp" not in str(ctx).lower():
                    name = ctx.get("scenario_set_name", "")
                    # prefer the one with the user prefix (longer name)
                    if len(name) > len(result.get("sim_set_name", "")):
                        result["sim_set_name"] = name
                        result["sim_set_id"]   = ctx.get("scenario_set_id", "")
                # idx=6: ETP
                if "etp_task_url" in ctx:
                    result["etp_url"]        = ctx.get("etp_task_url", "")
                    result["etp_batch_name"] = ctx.get("batch_name", "")
                    result["etp_tasktype"]   = ctx.get("tasktype", "")
            except Exception as e:
                logger.debug(f"decode idx={idx} error: {e}")

    # dn5 失败时 outputs 为空，尝试从 inputs 里的 processed_configs 恢复部分结果
    if not result:
        result = _parse_dn5_inputs_fallback(data)

    return result


def _parse_dn5_inputs_fallback(dn5_data: dict) -> dict:
    """
    当 dn5 outputs 为空（执行失败）时，从 dn5 的 inputs.processed_configs 中
    提取部分已完成步骤的结果（ALP 正样本集 ID、ETP batch 等）。
    """
    result = {}
    try:
        col = dn5_data.get("full_inputs", {}).get("literals", {}) \
                       .get("processed_configs", {}).get("collection", {}) \
                       .get("literals", [])
        for item in col:
            b64 = item.get("scalar", {}).get("binary", {}).get("value", "")
            if not b64:
                continue
            try:
                d = _dec(b64)
                task_name = d.get("fst_cla_task_name", "")
                rc = d.get("run_config", {})
                # ALP 正样本集（send_alp_data_to_etp_platform 步骤）
                if task_name == "send_alp_data_to_etp_platform":
                    ids = rc.get("alp_scenario_set_ids", [])
                    if ids:
                        result["alp_ok_id"] = ids[0]
                        result["alp_status"] = "done"
                # ETP batch（generate_tagged_scenario_set_from_etp 步骤）
                if task_name == "generate_tagged_scenario_set_from_etp":
                    batch = rc.get("etp_batch_name", "")
                    etype = rc.get("etp_tasktype", "")
                    # 只用本次运行的值（过滤掉模板里的历史值，按年份判断）
                    import re as _re
                    year_m = _re.search(r'(\d{4})', batch)
                    if year_m and int(year_m.group(1)) >= 2026 and batch:
                        result["etp_batch_name"] = batch
                        result["etp_tasktype"]   = etype
            except Exception:
                pass
    except Exception as e:
        logger.debug(f"dn5 inputs fallback error: {e}")
    return result


def parse_reinject_output(token: str, exec_id: str) -> Optional[str]:
    """
    扫 n1-0-dn6 (generate_tagged_scenario_set_from_etp_t) 的 outputs，
    返回回灌后的 scenario_set_id。
    """
    data = get_node_data(token, exec_id, "n1-0-dn6")
    if not data:
        return None
    literals = data.get("fullOutputs", {}).get("literals", {})
    for key, val in literals.items():
        col = val.get("collection", {}).get("literals", [])
        for item in col:
            b64 = item.get("scalar", {}).get("binary", {}).get("value", "")
            if b64:
                try:
                    d = _dec(b64)
                    ctx = d.get("data_task_ctx", {})
                    sid = ctx.get("scenario_set_id")
                    if sid:
                        return sid
                except Exception:
                    pass
    return None


# ── Scenario Set ID 解析 ─────────────────────────────────────────

import re as _re
ESS_BASE = "https://ess.momenta.works"
_SS_ID_RE = _re.compile(r'[0-9a-f]{24}')  # 24位 hex ObjectId


def resolve_scenario_set_id(input_str: str, dpi_token: str = None) -> dict:
    """
    将用户输入（URL / 纯ID / 纯名称）解析为 scenario_set_id。
    返回 {"id": "...", "name": "...", "repos": "..."}
    """
    s = input_str.strip()
    if not s:
        raise ValueError("输入不能为空")

    # 1. 纯 24 位 hex ID
    if _SS_ID_RE.fullmatch(s):
        return {"id": s, "name": "", "repos": ""}

    # 2. ESS URL：优先提取 id 参数
    import urllib.parse as _up
    if "ess.momenta.works" in s:
        try:
            qs = _up.parse_qs(_up.urlparse(s).query)
            sid = (qs.get("id") or [""])[0]
            name = (qs.get("name") or [""])[0]
            repos = (qs.get("repos") or ["AES_FST"])[0]
            if sid and _SS_ID_RE.fullmatch(sid):
                return {"id": sid, "name": name, "repos": repos}
            # 没有 id 参数，用 name+repos 查询
            if name and repos:
                from .scenario_service import _headers
                r = requests.get(
                    f"{ESS_BASE}/api/v1/scenario-set/{repos}/{_up.quote(name, safe='')}",
                    headers=_headers(), timeout=15, verify=False,
                )
                if r.status_code == 200:
                    sid = r.json().get("id", "")
                    if sid:
                        return {"id": sid, "name": name, "repos": repos}
                raise ValueError(f"ESS 查不到该 Scenario Set：{name} (repos={repos})")
        except ValueError:
            raise
        except Exception as e:
            raise ValueError(f"解析 ESS 链接失败：{e}")

    # 3. 把输入当做 name，用默认 repos AES_FST 查询
    from .scenario_service import _headers
    for repos in ("AES_FST", "ABSM_FST", "AES_MINING", "ADAS_FST"):
        try:
            r = requests.get(
                f"{ESS_BASE}/api/v1/scenario-set/{repos}/{_up.quote(s, safe='')}",
                headers=_headers(), timeout=15, verify=False,
            )
            if r.status_code == 200:
                sid = r.json().get("id", "")
                if sid:
                    return {"id": sid, "name": s, "repos": repos}
        except Exception:
            continue
    raise ValueError(f"找不到名为「{s}」的 Scenario Set，请直接粘贴 ESS 链接")


# ── DPI 任务创建 ──────────────────────────────────────────────────

TEMPLATE_EXEC_ID = "afjr4kzx4g9jtgh9gv2b"


def create_dpi_execution(
    token: str,
    scenario_set_id: str,
    name: str,
    name_prefix: str,
    template_exec_id: str = None,
) -> str:
    """
    克隆模板 execution，替换 scenario_set_id / name / name_prefix，
    创建新的 Flyte execution，返回新的 exec_id。
    """
    import copy
    s = _session(token)
    tpl_id = template_exec_id or TEMPLATE_EXEC_ID

    # 1. 拉取模板 spec
    r = s.get(f"{FLYTE_BASE}/api/v1/executions/ebm-infra/dev/{tpl_id}", timeout=15)
    r.raise_for_status()
    orig = r.json()
    spec = copy.deepcopy(orig["spec"])
    inputs = spec.get("inputs", {}).get("literals", {})

    # 2. 替换 scenario_set_id（在 bag_datasets 里）
    bag_lits = inputs.get("bag_datasets", {}).get("collection", {}).get("literals", [])
    for lit in bag_lits:
        generic = lit.get("scalar", {}).get("generic", {})
        if "alp_dataset" in generic:
            generic["alp_dataset"]["scenario_set_id"] = scenario_set_id

    # 3. 替换 fst_cla_configs 里的 name / scenario_set_name_prefix
    fst_lits = inputs.get("fst_cla_configs", {}).get("collection", {}).get("literals", [])
    for lit in fst_lits:
        rc = lit.get("scalar", {}).get("generic", {}).get("run_config", {})
        if "name" in rc:
            rc["name"] = name
        if "scenario_set_name_prefix" in rc:
            rc["scenario_set_name_prefix"] = name_prefix

    # 4. 创建新执行（兼容 camelCase 和 snake_case）
    launch_plan = spec.get("launchPlan") or spec.get("launch_plan")
    if not launch_plan:
        raise RuntimeError(f"无法从模板获取 launchPlan，spec keys: {list(spec.keys())}")
    body = {
        "project": "ebm-infra",
        "domain": "dev",
        "name": "",
        "spec": {
            "launchPlan": launch_plan,
            "inputs": spec["inputs"],
            "metadata": spec.get("metadata", {}),
        },
    }
    r2 = s.post(f"{FLYTE_BASE}/api/v1/executions", json=body, timeout=15)
    r2.raise_for_status()
    exec_id = r2.json().get("id", {}).get("name", "")
    if not exec_id:
        raise RuntimeError(f"创建执行失败: {r2.text}")
    return exec_id


# ── ETP 状态 ─────────────────────────────────────────────────────

def get_etp_batch_status(tasktype: str, batch_name: str, username: str, password: str) -> str:
    """查 ETP batch 状态，返回 COMPLETED / RUNNING / NOT_FOUND 等。"""
    r = requests.post(
        f"{ETP_BASE}/api/v2/genToken",
        json={"username": username, "password": password},
        timeout=15,
    )
    etp_token = r.json().get("data", {}).get("token", "")
    if not etp_token:
        return "AUTH_FAILED"
    r2 = requests.get(
        f"{ETP_BASE}/api/v2/batchstatus",
        params={"tasktype": tasktype, "batch_name": batch_name},
        headers={"authorization": etp_token},
        timeout=15,
    )
    d = r2.json().get("data") or {}
    if isinstance(d, list) and d:
        d = d[0]
    return d.get("status", "UNKNOWN")


# ── ALP 分析 ─────────────────────────────────────────────────────

def analyze_alp_task(alp_task_id: int, username: str, password: str) -> dict:
    """分析 ALP task 的 category 分布，复用 alp_service 逻辑。"""
    from .alp_service import _login as alp_login, _hdrs
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from collections import Counter

    BASE = "https://alp.momenta.works"
    AUTH = "https://data.momenta.works/auth/api/v1"
    s = requests.Session()
    r = s.post(AUTH + "/user/login",
               data={"username": username, "password": password, "platform": "atp"})
    token = r.json().get("token", "")
    if not token:
        return {"error": "ALP login failed"}

    hdrs = {"Authorization": token, "Content-Type": "application/json"}

    # get entity keys
    keys, page = [], 1
    while True:
        r2 = s.post(BASE + "/atp/api/v1/task/query_paginated",
                    headers=hdrs, json={"task_id": alp_task_id, "page_num": page, "page_size": 100})
        items = r2.json().get("data", {}).get("res_list", [])
        keys.extend(x["entity_key"] for x in items)
        if len(items) < 100:
            break
        page += 1

    counter = Counter()

    def fetch(ek):
        r3 = requests.post(BASE + "/atp/api/v1/task/get_event_result",
                           headers=hdrs, json={"task_id": alp_task_id, "entity_key": ek})
        d = r3.json()
        if d.get("code") == 0:
            for item in d["data"].get("content", []):
                for name, res in item.get("sim_checker_result", {}).get("checkers_result", {}).items():
                    if name != "Info":
                        return res.get("category", "Unknown")
            return "no_result"
        return "fetch_error"

    workers = min(20, max(5, len(keys) // 5))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for cat in ex.map(fetch, keys):
            counter[cat] += 1

    return {"total": len(keys), "categories": dict(counter.most_common())}


# ── 主轮询函数 ────────────────────────────────────────────────────

def poll_once(run: dict, dpi_token: str, dpi_username: str, dpi_password: str) -> dict:
    """
    对一条 pipeline_run 执行一次扫描，返回需要更新的字段 dict。
    run 至少需要有 exec_id。
    """
    exec_id = run.get("dpi_exec_id", "")
    if not exec_id:
        return {}

    updates = {}

    # 1. 整体执行状态
    try:
        phase = get_execution_phase(dpi_token, exec_id)
        updates["dpi_exec_phase"] = phase
        if phase == "SUCCEEDED":
            updates["dpi_status"] = "done"
        elif phase in ("FAILED", "ABORTED"):
            updates["dpi_status"] = "failed"
        else:
            updates["dpi_status"] = "in_progress"
    except Exception as e:
        logger.warning(f"get_execution_phase error: {e}")
        return updates

    # 2. 解析 dn5 outputs（ALP / Sim / ETP）
    try:
        dn5 = parse_dn5_outputs(dpi_token, exec_id)
        if dn5.get("alp_task_id") and not run.get("alp_task_id"):
            updates["alp_task_id"]  = dn5["alp_task_id"]
            updates["alp_url"]      = dn5.get("alp_task_url", "")
            updates["alp_status"]   = "in_progress"
        # 从 inputs fallback 提取的 ALP 正样本集（dn5 失败时）
        if dn5.get("alp_ok_id") and not run.get("alp_ok_id"):
            updates["alp_ok_id"]   = dn5["alp_ok_id"]
            updates["alp_status"]  = dn5.get("alp_status", "done")
        if dn5.get("etp_batch_name") and not run.get("etp_batch_name"):
            updates["etp_batch_name"] = dn5["etp_batch_name"]
            updates["etp_tasktype"]   = dn5.get("etp_tasktype", "")
            updates["etp_status"]     = "in_progress"
        if dn5.get("alp_status") == "SUCCESS" and run.get("alp_status") != "done":
            updates["alp_status"]     = "done"
            updates["alp_ok_name"]    = dn5.get("alp_pos_set_name", "")
            updates["alp_ok_id"]      = dn5.get("alp_pos_set_id", "")
            updates["alp_ok_count"]   = len(dn5.get("alp_error_cases", [])) or dn5.get("alp_ok_count", 0)
            # 触发 ALP 分析
            try:
                alp_analysis = analyze_alp_task(dn5["alp_task_id"], dpi_username, dpi_password)
                updates["alp_analysis"] = json.dumps(alp_analysis, ensure_ascii=False)
            except Exception as ae:
                logger.warning(f"ALP analysis error: {ae}")
        if dn5.get("sim_task_id") and not run.get("sim_task_id"):
            updates["sim_task_id"] = dn5["sim_task_id"]
            updates["sim_url"]     = f"https://simulation.momenta.works/batch/{dn5['sim_task_id']}"
            updates["sim_status"]  = "in_progress"
        if dn5.get("sim_set_id") and run.get("sim_status") != "done":
            updates["sim_status"]    = "done"
            updates["sim_out_name"]  = dn5.get("sim_set_name", "")
            updates["sim_out_id"]    = dn5.get("sim_set_id", "")
        if dn5.get("etp_url") and not run.get("etp_url"):
            updates["etp_url"]       = dn5["etp_url"]
            updates["etp_batch_name"]= dn5.get("etp_batch_name", "")
            updates["etp_tasktype"]  = dn5.get("etp_tasktype", "")
            updates["etp_status"]    = "in_progress"
    except Exception as e:
        logger.warning(f"parse dn5 error: {e}")

    # 3. ETP 完成检测
    etp_batch = run.get("etp_batch_name") or updates.get("etp_batch_name", "")
    etp_type  = run.get("etp_tasktype")  or updates.get("etp_tasktype", "")
    if etp_batch and etp_type and run.get("etp_status") != "done":
        try:
            etp_status = get_etp_batch_status(etp_type, etp_batch, dpi_username, dpi_password)
            if etp_status in ("COMPLETED", "DONE"):
                updates["etp_status"] = "done"
        except Exception as e:
            logger.warning(f"ETP status error: {e}")

    # 4. 回灌数据集（ETP完成后 DPI 继续跑）
    if run.get("etp_status") == "done" or updates.get("etp_status") == "done":
        try:
            reinject_id = parse_reinject_output(dpi_token, exec_id)
            if reinject_id and not run.get("reinject_set_id"):
                updates["reinject_set_id"]    = reinject_id
                updates["reinject_status"]    = "done"
                updates["fst_status"]         = "todo"   # 提示可以做 FST 写入了
        except Exception as e:
            logger.warning(f"reinject parse error: {e}")

    return updates
