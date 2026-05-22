"""
ALP Checker 任务提交服务
基于 task 1159180 的参数结构，提交 cp_aes_tagger_new_1114 Checker 任务
"""
import requests
from typing import List

ALP_BASE = "https://alp.momenta.works"
AUTH_URL = "https://data.momenta.works/auth/api/v1/user/login"

# 固定的 tagger 配置（来自 task 1159180 实测参数）
_TAGGER_CONFIG = {
    "tagger": [
        {
            "config": {
                "meta": {
                    "special_functions": {"skip_basecheck": True},
                    "params": {
                        "-cp_aes_tagger_1014": [
                            {
                                "aes_end_brake_max_cnt": 100.0,
                                "predict_egopose_step": 0.02,
                                "is_cfdi": False,
                                "aes_start_brake_flation": 0.0,
                                "aes_end_brake_min_acc": -5.0,
                                "aes_start_brake_min_acc": -5.0,
                                "aes_start_brake_min_jerk": -6.0,
                                "aes_end_brake_min_jerk": -6.0,
                                "aes_start_brake_max_cnt": 100.0,
                                "aes_end_brake_flation": 0.0,
                                "min_abs_steer": 0.175,
                                "max_abs_steer": 0.26,
                            }
                        ]
                    },
                },
                "name": "cp_aes_tagger_new_1114",
            }
        }
    ],
    "enable_entity_log": False,
    "engine": "tos_job",
    "env": "cpp",
    "product": "CP PPM",
    "loader_config": {"local_dir": True, "skip_gen_meta": True},
    "engine_config": {
        "tos_worker_type": "job",
        "tos_job_image": "artifactory.momenta.works/docker-atcraft-dev/operator-checker-worker-test-prod:msim_bag_checker-master-20260414092316",
    },
    "saver": [{"name": "AtpDefaultSaver", "config": {}}],
}


def _login(username: str, password: str) -> str:
    r = requests.post(AUTH_URL, data={
        "username": username, "password": password, "platform": "atp"
    }, timeout=15)
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"ALP 登录失败: {d.get('message')}")
    return d["token"]


def submit_checker_task(
    task_name: str,
    scenario_set_ids: List[str],
    username: str,
    password: str,
    priority: str = "AUTO",
) -> dict:
    """
    提交一个 ALP Checker 任务。
    返回 {"task_id": ..., "task_url": ..., "name": ...}
    """
    token = _login(username, password)
    hdrs = {"Authorization": token, "Content-Type": "application/json"}

    body = {
        "name": task_name,
        "description": f"Submitted via Junhao BOX. {len(scenario_set_ids)} scenario set(s).",
        "data_type": "scenario_set_id",
        "data_list": scenario_set_ids,
        "task_type": "Checker",
        "config": _TAGGER_CONFIG,
        "checker_names": ["cp_aes_tagger_new_1114"],
        "params": [],
        "priority": priority,
        "sim_checker_commit_id": "",
    }

    r = requests.post(
        f"{ALP_BASE}/atp/api/v1/task/submit_task",
        headers=hdrs, json=body, timeout=30,
    )
    r.raise_for_status()
    d = r.json()
    if d.get("code") != 0:
        raise RuntimeError(f"提交失败: {d.get('message')}")

    task_id = d.get("data", {}).get("task_id") or d.get("data")
    task_url = f"https://alp.momenta.works/task-detail/?id={task_id}" if task_id else ""
    return {"task_id": task_id, "task_url": task_url, "name": task_name}
