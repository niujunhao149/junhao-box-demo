#!/usr/bin/env python3
"""
SimICT Task Creator
Create simulation tasks with flexible input formats and multiple simulation modes
"""

import sys
import os
from pathlib import Path

# Force unbuffered stdout for background task visibility
sys.stdout.reconfigure(line_buffering=True)

# ============================================================================
# 虚拟环境自举 - 首次运行自动创建 venv 并安装依赖，后续直接使用 venv
# ============================================================================
SKILL_DIR = Path(__file__).resolve().parent.parent
VENV_DIR = SKILL_DIR / ".venv"
MOMENTA_INDEX = "https://artifactory.momenta.works/artifactory/api/pypi/pypi-momenta/simple"
PL_INDEX = "https://artifactory.momenta.works/artifactory/api/pypi/pypi-pl/simple"

def _get_skill_version():
    """Read skill version from doc/VERSION."""
    version_file = SKILL_DIR / "doc" / "VERSION"
    if version_file.is_file():
        return version_file.read_text().strip()
    return "unknown"


def _in_venv():
    """Check if currently running inside the skill's venv."""
    return hasattr(sys, "prefix") and Path(sys.prefix).resolve() == VENV_DIR.resolve()


def _venv_is_ready():
    """Check if venv exists, dependencies installed, and version matches."""
    marker = VENV_DIR / ".installed"
    venv_python = VENV_DIR / "bin" / "python3"
    if not (venv_python.is_file() and marker.is_file()):
        return False
    # 版本不匹配时强制重建
    installed_version = marker.read_text().strip()
    current_version = _get_skill_version()
    if installed_version != current_version:
        print(f"🔄 检测到版本更新: {installed_version} → {current_version}，重建虚拟环境...")
        return False
    return True


def _create_venv_and_install():
    """Create venv and install all dependencies into it."""
    import subprocess
    import shutil

    # 清理可能残留的不完整 venv
    if VENV_DIR.exists():
        print(f"🧹 清理不完整的虚拟环境...")
        shutil.rmtree(VENV_DIR)

    print(f"🔧 首次运行，创建虚拟环境: {VENV_DIR}")

    # 使用 subprocess 调用 venv 模块，以便捕获 ensurepip 缺失等错误
    result = subprocess.run(
        [sys.executable, "-m", "venv", "--prompt", "simict-task-creator", str(VENV_DIR)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("❌ 创建虚拟环境失败:")
        print(result.stderr)
        # 给出具体的修复建议
        if "ensurepip" in result.stderr:
            py_ver = f"{sys.version_info.major}.{sys.version_info.minor}"
            print(f"\n请先安装 venv 支持包:")
            print(f"   apt install python{py_ver}-venv")
            print(f"然后重新运行即可。")
        sys.exit(1)

    venv_python = str(VENV_DIR / "bin" / "python3")

    # 升级 pip（避免旧版 resolver 问题）
    subprocess.run(
        [venv_python, "-m", "pip", "install", "-q", "--upgrade", "pip"],
        capture_output=True,
    )

    # 安装 Momenta 内部包及全部依赖
    # pyevents/pyfeishu 从 pypi-momenta + pypi-pl 安装，传递依赖从公共 PyPI 解析
    # 注意：不要用 --force-reinstall，否则 pip resolver 会评估所有 dev 版本导致 ResolutionImpossible
    print("📦 安装 pyevents, pyfeishu 及依赖...")
    result = subprocess.run(
        [venv_python, "-m", "pip", "install", "-q",
         "pyevents", "pyfeishu",
         "--index-url", MOMENTA_INDEX,
         "--extra-index-url", PL_INDEX,
         "--extra-index-url", "https://pypi.org/simple/"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print("❌ 安装依赖失败:")
        print(result.stderr)
        sys.exit(1)

    # 修复 pydantic 兼容性：pyevents 锁定 pydantic==1.10.12，但该版本不兼容 Python 3.12+
    # 升级到 1.10.18+ 修复 ForwardRef._evaluate() 问题，API 完全兼容
    print("📦 修复 pydantic 兼容性...")
    subprocess.run(
        [venv_python, "-m", "pip", "install", "-q", "pydantic>=1.10.18,<2"],
        capture_output=True, text=True,
    )

    # 写入版本标记（版本变更时自动触发重建）
    version = _get_skill_version()
    (VENV_DIR / ".installed").write_text(version + "\n")
    print(f"✅ 虚拟环境创建完成 (v{version})")


def _bootstrap_venv():
    """Ensure we're running inside the skill venv; if not, re-exec."""
    if _in_venv():
        return  # 已在 venv 中，继续执行

    # 如果 venv 不存在、不完整或损坏，重新创建
    if not _venv_is_ready():
        _create_venv_and_install()

    # 用 venv 的 Python 重新执行本脚本
    venv_python = str(VENV_DIR / "bin" / "python3")
    os.execv(venv_python, [venv_python] + sys.argv)


_bootstrap_venv()
# ============================================================================

import json
import os
import re
import subprocess
import time
from typing import Dict, List, Optional, Tuple

import requests
import yaml

# API 请求超时配置 (秒)
API_TIMEOUT = 30
from pyevents.event_library_services import (
    import_scenario_set_from_events,
    search_events_new_v2,
)
from pyevents.utils.enums import EventSearchReturnMetaV2
# pyfeishu 内部版 (含 auth/toolsets), 缺失时降级运行
try:
    from pyfeishu.auth.feishu_project_auth_helper import ProjectAuth
    from pyfeishu.toolsets.project.user_toolset.user_toolset import UserToolSet
    from pyfeishu.toolsets.project.work_item_toolset.work_item_toolset import (
        WorkItemToolset,
    )
    HAS_PYFEISHU_PROJECT = True
except ImportError:
    ProjectAuth = None
    UserToolSet = None
    WorkItemToolset = None
    HAS_PYFEISHU_PROJECT = False

# Constants
KEYCLOAK_URL = "https://keycloak-prod-cla.mmtwork.com/auth"
REALM_NAME = "momenta-prod"
CLIENT_ID = "ess-client"
TOKEN_URL = f"{KEYCLOAK_URL}/realms/{REALM_NAME}/protocol/openid-connect/token"

# Keycloak 共享文件缓存
_KC_TOKEN_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".cache", "skill_tokens")
_KC_TOKEN_CACHE_FILE = os.path.join(_KC_TOKEN_CACHE_DIR, "keycloak_ess_token")

FEISHU_PROJECT_ID = os.getenv("FEISHU_PROJECT_ID", "MII_EXAMPLE")
FEISHU_SECRET = os.getenv("FEISHU_SECRET", "EXAMPLE_SECRET_PLACEHOLDER")

# Default configurations (fallback if config file not found)
CHECKER_IMAGE = "artifactory.momenta.works/docker-msd/checker_ci_build:cp-dev_20260130_a92d1b3"
DEFAULT_VEHICLE = "MKZ-E7084S"
DEFAULT_PRIORITY = 5
# 默认用户 (可通过环境变量 SIMICT_DEFAULT_USER 覆盖)
DEFAULT_USER = os.environ.get("SIMICT_DEFAULT_USER", "on_0f3159dcfb908ac864465fd4802a9244")

DISABLED_CHECKERS = [
    "-cpplde_rotl", "-sudden_hs", "-cone_mis_change_lane",
    "-cp_longitudinal_cutin_cpi", "-cp_tfl_run_red_light", "-cutin_cldcpd",
    "-cp_tfl_no_light_go_misbrake", "-general_braking_fp", "-avoid_pass",
    "-cp_change_lane_finish_2", "-cp_tfl_green_flash_go", "-vru_cldcpd",
    "-cldcprdsf", "-ddldlk_m", "-latsk_strai", "-avoid_hitcpd", "-rotl",
    "-cp_tfl_sg_green_light_start", "-cp_tfl_sg_run_red", "-sghard",
    "-pass_intersection_collision_risk", "-pass_intersection_lane_selection",
    "-sgunstable", "-cpebs_h_m", "-cp_lateral_cll_soft",
    "-cp_tfl_red_wait_ab_start", "-cp_tfl_sg_decelerate_behavior",
    "-cp_lateral_road_border_collision", "-cp_tfl_sg_stop_2nd_start",
    "-cp_tfl_sg_green_light_go_braking_fp", "-new_lon_acldcpd",
    "-latsk_inter", "-new_lon_ccrbcpd", "-arbdmb", "-ddldlk_h_m",
    "-braking_fp", "-cp_change_lane_collision_risk", "-braking_fp_hard",
    "-kcb", "-latsk", "-vru_acldcpd", "-conebrakefp_hard", "-cpebs_m",
    "-hard_cldcpd", "-alcback_all", "-conebrakefp", "-avoid_quit_time",
    "-cutin_collision_cpi", "-bslc"
]

# CI BUILD 默认配置 (CP 产品仿真复现最佳实践)
DEFAULT_CI_BUILD_CONFIG = {
    "build_config_dir": "Devcar",
    "global_envs": {
        "APA_MAINLINE": "yes",
        "NPP_MODE": "MAX_SELECTOR"
    },
    "modules": [],
    "system_yaml_name": "system.yaml"
}

# 产品配置 (Product-specific configurations) - Legacy fallback
ADAS_CHECKER_IMAGE = "artifactory.momenta.works/docker-msd/checker_ci_build:cp-dev_20260212_204071e"
ADAS_CI_BUILD_CONFIG = {
    "build_config_dir": "Devcar",
    "global_envs": {"APA_MAINLINE": "yes", "NPP_MODE": "MAX_SELECTOR"},
    "modules": [],
    "system_yaml_name": "system.yaml",
}

PRODUCT_CONFIGS = {
    # Pilot 产品组
    "CP": {
        "fst_repo": "CP_FST",
        "fst_path": "CP_FST",
        "default_method": "ci_build",
        "template_task": "https://simulation.momenta.works/experiment/version_test/698b153c2b6608085749f1a7",
        "checker_image": CHECKER_IMAGE,
        "product_group": "Pilot",
        "ci_build_config": DEFAULT_CI_BUILD_CONFIG,
    },
    "NP": {
        "fst_repo": "L3_CFST",
        "fst_path": "降级",
        "default_method": "template",
        "template_task": "https://simulation.momenta.works/experiment/version_test/698b153c2b6608085749f1a7",
        "template_task_em": "https://simulation.momenta.works/experiment/version_test/6969ac39eee4890fe9601b4d",
        "checker_image": CHECKER_IMAGE,
        "product_group": "Pilot",
    },
    "MNP": {
        "fst_repo": "MNP_FST",
        "fst_path": "FIT-高优问题仿真",
        "default_method": "ci_build",
        "template_task": "https://simulation.momenta.works/experiment/version_test/698b153c2b6608085749f1a7",
        "checker_image": CHECKER_IMAGE,
        "product_group": "Pilot",
        "ci_build_config": DEFAULT_CI_BUILD_CONFIG,
    },
    # ADAS 产品组
    "CMSR": {
        "fst_repo": "CMSR_FST",
        "fst_path": "OpenClaw任务目录/OpenClaw任务数据",
        "default_method": "template",
        "template_task": "https://simulation.momenta.works/experiment/version_test/69a15606df81b2cb8bb1da54",
        "checker_image": ADAS_CHECKER_IMAGE,
        "which_car": "LC6-EDX9238",
        "profile": "ebm_r6_ebm_mainline",
        "product_group": "ADAS",
        "ci_build_config": ADAS_CI_BUILD_CONFIG,
    },
    "IHC": {
        "fst_repo": "IHC_FST_Mainline",
        "fst_path": "废弃-OpenClaw任务目录/OpenClaw任务数据",
        "default_method": "template",
        "template_task": "https://simulation.momenta.works/experiment/version_test/69a1569b1f8bc95a4af6a490",
        "checker_image": ADAS_CHECKER_IMAGE,
        "which_car": "LC6-EDX9238",
        "profile": "ihc-ebm-r6-slp",
        "product_group": "ADAS",
        "ci_build_config": ADAS_CI_BUILD_CONFIG,
    },
    "LSS": {
        "fst_repo": "LSS_FST",
        "fst_path": "OpenClaw任务目录",
        "default_method": "template",
        "template_task": "https://simulation.momenta.works/experiment/version_test/69a1577a1f8bc95a4af6bb60",
        "checker_image": ADAS_CHECKER_IMAGE,
        "which_car": "LC6-EDX9238",
        "profile": "ebm-r6",
        "product_group": "ADAS",
        "ci_build_config": ADAS_CI_BUILD_CONFIG,
    },
    "BSD": {
        "fst_repo": "Alarm_Mainline",
        "fst_path": "OpenClaw任务目录",
        "default_method": "template",
        "template_task": "https://simulation.momenta.works/experiment/version_test/69a157e2adc5b50815d91128",
        "checker_image": ADAS_CHECKER_IMAGE,
        "which_car": "LC6-EDX9238",
        "profile": "ebm-r6",
        "product_group": "ADAS",
        "ci_build_config": ADAS_CI_BUILD_CONFIG,
    },
    "AES": {
        "fst_repo": "AES_FST",
        "fst_path": "OpenClaw任务目录",
        "default_method": "template",
        "template_task": "https://simulation.momenta.works/experiment/version_test/69a1584fd90c464f222600e5",
        "checker_image": ADAS_CHECKER_IMAGE,
        "which_car": "LC6-EDX9238",
        "profile": "default",
        "product_group": "ADAS",
        "ci_build_config": ADAS_CI_BUILD_CONFIG,
    },
    "ESS": {
        "fst_repo": "ESS_FST_Mainline",
        "fst_path": "OpenClaw任务目录",
        "default_method": "template",
        "template_task": "https://simulation.momenta.works/experiment/version_test/69a158b4adc5b50815d92638",
        "checker_image": ADAS_CHECKER_IMAGE,
        "which_car": "LC6-EDX9238",
        "profile": "ebm_r6",
        "product_group": "ADAS",
        "ci_build_config": ADAS_CI_BUILD_CONFIG,
    },
    "SLIF": {
        "fst_repo": "SLIF_FST",
        "fst_path": "OpenClaw任务目录",
        "default_method": "template",
        "template_task": "https://simulation.momenta.works/experiment/version_test/69a15932df81b2cb8bb210dd",
        "checker_image": ADAS_CHECKER_IMAGE,
        "which_car": "LC6-EDX9238",
        "profile": "ebm_r6_ebm_sim",
        "product_group": "ADAS",
        "ci_build_config": ADAS_CI_BUILD_CONFIG,
    },
    "ABSM": {
        "fst_repo": "ABSM_FST",
        "fst_path": "OpenClaw任务目录",
        "default_method": "template",
        "template_task": "https://simulation.momenta.works/experiment/version_test/69a15998d90c464f22261c69",
        "checker_image": ADAS_CHECKER_IMAGE,
        "which_car": "LC6-EDX9238",
        "profile": "ebm_r6",
        "product_group": "ADAS",
        "ci_build_config": ADAS_CI_BUILD_CONFIG,
    },
    "DOW": {
        "fst_repo": "Alarm_Mainline",
        "fst_path": "OpenClaw任务目录",
        "default_method": "template",
        "template_task": "https://simulation.momenta.works/experiment/version_test/69a15ac91f8bc95a4af6f8eb",
        "checker_image": ADAS_CHECKER_IMAGE,
        "which_car": "LC6-EDX9238",
        "profile": "ebm-r6",
        "product_group": "ADAS",
        "ci_build_config": ADAS_CI_BUILD_CONFIG,
    },
    # TODO: ADB 需求收集中 — template_task 为占位符，需从团队获取真实 template ID 后启用
    "ADB": {
        "fst_repo": "ADAS_FST",
        "fst_path": "ADB",
        "default_method": "template",
        "template_task": "https://simulation.momenta.works/experiment/version_test/[ADB_TEMPLATE_ID]",
        "checker_image": ADAS_CHECKER_IMAGE,
        "which_car": "LC6-EDX9238",
        "profile": "ebm-r6",
        "product_group": "ADAS",
        "ci_build_config": ADAS_CI_BUILD_CONFIG,
    },
}


def load_product_configs(config_path: Optional[str] = None) -> Dict:
    """
    Load product configurations from YAML file
    
    Args:
        config_path: Path to product_configs.yaml. If None, searches in:
                    1. Current working directory
                    2. config/ directory (relative to skill root)
                    3. Script directory (src/)
                    4. Falls back to hardcoded PRODUCT_CONFIGS
    
    Returns:
        Dictionary with structure:
        {
            'projects': {...},
            'defaults': {...},
            'loaded_from': str (path or 'hardcoded')
        }
    """
    if config_path is None:
        # Search for config file
        skill_root = Path(__file__).parent.parent
        search_paths = [
            Path.cwd() / "product_configs.yaml",
            skill_root / "config" / "product_configs.yaml",
            Path(__file__).parent / "product_configs.yaml",
        ]
        
        for path in search_paths:
            if path.exists():
                config_path = str(path)
                break
    
    if config_path and Path(config_path).exists():
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                config = yaml.safe_load(f)
            
            print(f"✅ Loaded product configs from: {config_path}")
            config['loaded_from'] = config_path
            return config
        except Exception as e:
            print(f"⚠️  Failed to load config from {config_path}: {e}")
            print("   Falling back to hardcoded configurations")
    
    # Fallback to hardcoded configs
    print("⚠️  Using hardcoded product configurations (no YAML file found)")
    return {
        'projects': {
            'default': {
                'name': 'CP/NP/MNP Project',
                'products': PRODUCT_CONFIGS
            }
        },
        'defaults': {
            'vehicle': DEFAULT_VEHICLE,
            'priority': DEFAULT_PRIORITY,
            'disabled_checkers': DISABLED_CHECKERS,
        },
        'loaded_from': 'hardcoded'
    }


class SimICTTaskCreator:
    """Create SimICT simulation tasks"""

    def __init__(self, config_path: Optional[str] = None, project: str = "default"):
        """
        Initialize task creator

        Args:
            config_path: Path to product_configs.yaml
            project: Project name from config file (default: "default")
        """
        if HAS_PYFEISHU_PROJECT:
            self.auth = ProjectAuth(FEISHU_PROJECT_ID, FEISHU_SECRET)
            self.user_tool = UserToolSet(auth=self.auth)
            self.work_item_tool = WorkItemToolset(auth=self.auth)
        else:
            self.auth = None
            self.user_tool = None
            self.work_item_tool = None
        self._token_cache = None
        self._token_expiry = 0
        self._vehicle_cache = {}  # Cache: vehicle_type -> device_box_id

        # Load configurations
        self.config = load_product_configs(config_path)
        self.project = project
        
        # Validate project exists
        if project not in self.config['projects']:
            available = ', '.join(self.config['projects'].keys())
            raise ValueError(
                f"Project '{project}' not found in config. "
                f"Available projects: {available}"
            )
        
        self.project_config = self.config['projects'][project]
        self.products = self.project_config['products']
        self.defaults = self.config.get('defaults', {})
        
        print(f"🎯 Active project: {self.project_config.get('name', project)}")
        print(f"📦 Available products: {', '.join(self.products.keys())}")

    def get_token(self) -> str:
        """Get Keycloak access token with caching (memory > file > request)"""
        # 1. 内存缓存
        if self._token_cache and time.time() < self._token_expiry:
            return self._token_cache

        # 2. 文件缓存
        try:
            if os.path.exists(_KC_TOKEN_CACHE_FILE):
                with open(_KC_TOKEN_CACHE_FILE, "r") as f:
                    cached = json.load(f)
                expire_time = cached.get("expire_time", 0)
                if time.time() < expire_time - 300:
                    self._token_cache = cached["access_token"]
                    self._token_expiry = expire_time - 300
                    return self._token_cache
        except Exception:
            pass

        # 3. 请求新 token
        username = os.environ.get("SIMICT_KC_USER") or os.environ.get("KEYCLOAK_USERNAME") or "cp_system_gpt"
        password = os.environ.get("SIMICT_KC_PASS") or os.environ.get("KEYCLOAK_PASSWORD") or "csg_2025"

        data = {
            "client_id": CLIENT_ID,
            "username": username,
            "password": password,
            "grant_type": "password",
        }

        response = requests.post(TOKEN_URL, data=data, timeout=API_TIMEOUT)
        response.raise_for_status()
        tokens = response.json()

        self._token_cache = tokens["access_token"]
        expires_in = tokens.get("expires_in", 300)
        self._token_expiry = time.time() + expires_in - 60

        # 写回文件缓存（best-effort）
        try:
            os.makedirs(_KC_TOKEN_CACHE_DIR, exist_ok=True)
            cache_data = {
                "access_token": self._token_cache,
                "expire_time": time.time() + expires_in,
                "username": username,
                "client_id": CLIENT_ID,
            }
            with open(_KC_TOKEN_CACHE_FILE, "w") as f:
                json.dump(cache_data, f, indent=2)
            os.chmod(_KC_TOKEN_CACHE_FILE, 0o600)
        except Exception:
            pass

        return self._token_cache

    def resolve_user(self, feishu_union_id: str) -> str:
        """Resolve Feishu union ID to email username"""
        try:
            user_info = self.user_tool.query_user_info(out_ids=[feishu_union_id])
            if user_info:
                email = user_info[0].get("email", "")
                username = email.split("@")[0]
                if ".ext" in username:
                    return "hongzhou.liu"
                return username
        except Exception as e:
            print(f"Failed to resolve user: {e}")

        return "hongzhou.liu"

    def parse_input(self, bag_input: str) -> str:
        """
        Parse various input formats to event ID or scenario set ID

        Supported formats:
        - Event ID: 69a5875d77957753ad8024c5
        - Mviz link: https://mviz.momenta.works/player/v5/?meta=...
        - ESS link: https://ess.momenta.works/ui/v1/event?id=...
        - Bag MD5: abc123...
        - Scenario set ID: 667e5f64250b7b126b965e92
        - Case link: https://feishu.cn/case/detail/...
        - EQL query: bag_name like '%test%'
        """
        # Mviz link
        if "mviz." in bag_input:
            if "/event/" in bag_input:
                match = re.search(r"/event/([a-zA-Z0-9]+)/", bag_input)
                return match.group(1) if match else bag_input

            if "/scenario-set" in bag_input:
                match = re.search(r"/scenario-set/([a-zA-Z0-9]+)/([a-zA-Z0-9]+)", bag_input)
                return match.group(2) if match else bag_input

            # MD5 query param
            match = re.search(r"bag_md5=([a-fA-F0-9]+)", bag_input)
            if match:
                event = self._get_event_by_md5(match.group(1))
                return event["id"]

        # ESS link
        if "ess." in bag_input:
            match = re.search(r"id=([a-z0-9]+)", bag_input)
            if match:
                return match.group(1)

        # Feishu case link
        if "case/" in bag_input:
            match = re.search(r"detail/([a-z0-9]+)", bag_input)
            if match:
                case_id = match.group(1)
                return self._case_id_to_md5(case_id)

        # CLA link
        if "cla." in bag_input:
            match = re.search(r"data/([a-z0-9]+)", bag_input)
            if match:
                return match.group(1)

        # EQL query (contains spaces)
        if " " in bag_input:
            scenario_name = f"CPBot_Query_{time.strftime('%Y%m%d%H%M%S')}"
            return self._create_scenario_from_eql(bag_input, scenario_name)

        # Direct event ID / scenario set ID / bag name / MD5
        return bag_input

    def _get_event_by_md5(self, md5: str) -> Dict:
        """Query event by bag MD5"""
        headers = {"Authorization": f"Bearer {self.get_token()}"}
        data = {"eql": f"cla_md5 IN ('{md5}')"}
        response = requests.post(
            "https://ess.momenta.works/api/v1/event/_search",
            headers=headers,
            json=data,
        )
        response.raise_for_status()
        return response.json()["content"][0]

    def _case_id_to_md5(self, case_id: str) -> str:
        """Query bag MD5 from Feishu case ID"""
        cases = self.work_item_tool.query_work_items_v2(
            project_key="65d6c20802cf2c99dcba17b9",
            work_item_type_key="65fab2c09169e28c18e6ff7f",
            work_item_ids=[case_id],
        )

        for case in cases:
            for field in case.get("fields", []):
                if field.get("field_alias") == "bag_md5":
                    return field.get("field_value", "")

        raise ValueError(f"No bag_md5 found for case {case_id}")

    def _create_scenario_from_eql(self, eql: str, name: str) -> str:
        """Create scenario set from EQL query"""
        events = search_events_new_v2(
            eql=eql, return_meta=[EventSearchReturnMetaV2.EVENT_ID]
        )
        event_ids = [str(event.get("event_id")) for event in events]

        result = import_scenario_set_from_events(
            repos="CP_FST", name=name, event_ids=event_ids
        )
        return result["id"]

    def _normalize_separators(self, bag_input: str) -> str:
        """
        统一处理各种分隔符，将多种分隔符格式转换为标准的英文逗号分隔

        支持的分隔符：
        - 英文逗号: ,
        - 中文顿号: 、
        - 中文/英文分号: ；;
        - 换行符: \n \r
        - 制表符: \t
        - 全角逗号: ，
        - 全角分号: ；
        - 空格: 仅当检测到多个有效输入时才视为分隔符

        Returns:
            标准化后的输入字符串（用英文逗号分隔）
        """
        import re

        original_input = bag_input

        # 1. 替换明确的非空格分隔符（不会造成歧义的分隔符）
        separators = {
            '、': ',',   # 中文顿号
            '；': ',',   # 中文分号
            ';': ',',    # 英文分号
            '\n': ',',   # 换行符
            '\r': ',',   # 回车符
            '\t': ',',   # 制表符
            '，': ',',   # 全角逗号
        }

        for old, new in separators.items():
            bag_input = bag_input.replace(old, new)

        # 2. 智能处理空格：检测是否有多个有效输入特征
        # 特征包括：多个URL、多个24位十六进制ID（MongoDB ObjectId）、多个32位MD5
        has_multiple_urls = bag_input.count('http') > 1 or bag_input.count('mviz') > 1 or bag_input.count('ess.momenta') > 1

        # 检测多个 Event ID (24位十六进制，MongoDB ObjectId 格式)
        event_id_pattern = r'\b[0-9a-f]{24}\b'
        event_ids_found = re.findall(event_id_pattern, bag_input, re.IGNORECASE)
        has_multiple_ids = len(event_ids_found) > 1

        # 检测多个 MD5 (32位十六进制)
        md5_pattern = r'\b[0-9a-f]{32}\b'
        md5_found = re.findall(md5_pattern, bag_input, re.IGNORECASE)
        has_multiple_md5 = len(md5_found) > 1

        # 检测多个 bag 文件名（包含 .bag 后缀）
        has_multiple_bags = bag_input.count('.bag') > 1

        # 如果检测到多个有效输入，则将所有空白字符视为分隔符
        if has_multiple_urls or has_multiple_ids or has_multiple_md5 or has_multiple_bags:
            # 在修改 bag_input 前保存计数
            url_count = bag_input.count('http')
            bag_count = bag_input.count('.bag')

            # \s 匹配所有空白字符（空格、制表符、换行等）
            # \u3000 是全角空格
            bag_input = re.sub(r'[\s\u3000]+', ',', bag_input)

            # 输出检测信息（帮助调试）
            detected_count = (
                len(event_ids_found) if has_multiple_ids else
                (url_count if has_multiple_urls else
                 (len(md5_found) if has_multiple_md5 else
                  (bag_count if has_multiple_bags else 0)))
            )
            if detected_count > 1:
                print(f"   📊 检测到 {detected_count} 个输入项，已统一分隔符")

        # 3. 清理多余的逗号
        # 移除连续的逗号、首尾的逗号
        bag_input = re.sub(r',+', ',', bag_input)
        bag_input = bag_input.strip(',')

        # 4. 验证结果：确保至少有一个有效输入
        if not bag_input or bag_input.isspace():
            print(f"⚠️  输入标准化后为空，使用原始输入")
            return original_input.strip()

        return bag_input

    def create_scenario_set(
        self,
        bag_input: str,
        vehicle_name: Optional[str] = None,
        roi_start: Optional[float] = None,
        roi_end: Optional[float] = None,
        product: Optional[str] = None,
    ) -> Tuple[str, str, str, str]:
        """
        Create scenario set from bag input

        Args:
            bag_input: Event ID, MD5, link, etc.
            vehicle_name: Vehicle box ID (optional)
            roi_start: ROI start time (optional, default: 10)
            roi_end: ROI end time (optional, default: 25)
            product: Force product type (CP/NP/MNP, optional, auto-detect if None)

        Returns:
            (scenario_id, scenario_name, vehicle_name, product)
        """
        # 统一分隔符格式（支持中文顿号、空格、分号等多种分隔符）
        bag_input = self._normalize_separators(bag_input)

        # Handle comma-separated list (先检查是否有多个输入)
        if "," in bag_input:
            # 多个输入：按逗号分割，对每个部分单独解析
            event_ids = []
            for part in bag_input.split(","):
                if part.strip():
                    parsed = self.parse_input(part.strip())
                    # Try to find event by ID first
                    events = search_events_new_v2(ids=[parsed])
                    if not events:
                        # Fallback: try as event_name or bag_name/cla_md5 (use exact match for better performance)
                        eql = f"name = '{parsed}' or bag_name = '{parsed}' or cla_md5 in ('{parsed}')"
                        events = search_events_new_v2(eql=eql, return_meta=[EventSearchReturnMetaV2.EVENT_ID])

                    if events:
                        event_id = events[0].get('event_id') or events[0].get('id')
                        if event_id:
                            event_ids.append(event_id)
                        else:
                            print(f"⚠️  Event 缺少 ID 字段: {part.strip()}")
                    else:
                        print(f"⚠️  无法找到事件: {part.strip()}")

            if not event_ids:
                raise ValueError(f"未找到任何有效事件，输入: {bag_input}")

            first_event = search_events_new_v2(ids=[event_ids[0]])[0]
        else:
            # 单个输入：直接解析
            normalized = self.parse_input(bag_input)
            event_ids = [normalized]
            events = search_events_new_v2(ids=[normalized])
            if not events:
                # Try as event_name or bag_name/cla_md5 (use exact match for better performance)
                eql = f"name = '{normalized}' or bag_name = '{normalized}' or cla_md5 in ('{normalized}')"
                events = search_events_new_v2(eql=eql, return_meta=[EventSearchReturnMetaV2.EVENT_ID])

                # If still no events found, provide helpful error message
                if not events:
                    print(f"\n❌ 无法找到对应的 Event")
                    print(f"输入: {bag_input}")
                    print(f"已尝试: Event ID, Event Name, Bag MD5, Scenario Set ID, Bag Name")
                    print(f"\n💡 支持的输入格式:")
                    print(f"  - Event ID: 69a5875d77957753ad8024c5")
                    print(f"  - Event Name: V223M-4239-CP测试-20260310-200547")
                    print(f"  - ESS Link: https://ess.momenta.works/events/detail?id=xxx")
                    print(f"  - Mviz Link: https://mviz.momenta.works/player/v5/?meta=...")
                    print(f"  - Bag MD5: 7993d1038b97512aafe2006064fed50e")
                    print(f"  - Scenario Set ID: 667e5f64250b7b126b965e92")
                    raise ValueError(f"Event not found for input: {bag_input}")

            first_event = events[0]

        # Extract metadata
        vehicle_type = first_event.get("vehicle_type")
        hostname = first_event.get("hostname")
        
        # Determine product
        if product is None:
            product = self._get_product_from_event(first_event.get("id", ""))
        
        # Get product config
        prod_config = self.products.get(product)
        if not prod_config:
            raise ValueError(
                f"Product '{product}' not found in project '{self.project}'. "
                f"Available: {', '.join(self.products.keys())}"
            )
        fst_repo = prod_config["fst_repo"]

        # Create scenario set
        scenario_name = f"{first_event.get('id', 'unknown')}_{time.strftime('%Y%m%d%H%M%S')}".replace(
            " ", "_"
        ).replace("-", "_")

        # Build sim_meta with custom ROI if provided
        roi_start = roi_start if roi_start is not None else 10
        roi_end = roi_end if roi_end is not None else 25
        ddp_replay_time = max(roi_start - 0.3, 0)
        evaluate_start_time = roi_start + 1
        evaluate_end_time = roi_end

        sim_meta = f"""
roi_start: {roi_start}
roi_end: {roi_end}
ddp_replay_time: {ddp_replay_time}
evaluate_start_time: {evaluate_start_time}
evaluate_end_time: {evaluate_end_time}
"""

        # Resolve vehicle early (optimization: skip query if vehicle_name provided)
        if vehicle_name:
            resolved_vehicle = vehicle_name
        elif vehicle_type:
            # Query vehicle info - uses cache if available
            resolved_vehicle = self._query_vehicle_from_simulation(vehicle_type)
        else:
            resolved_vehicle = DEFAULT_VEHICLE

        # Create scenario set (this is the time-consuming operation)
        result = import_scenario_set_from_events(
            repos=fst_repo,
            name=scenario_name,
            event_ids=event_ids,
            sim_meta=sim_meta,
        )

        return (
            result["id"],
            scenario_name,
            resolved_vehicle,
            product,
        )

    def _get_product_from_event(self, event_id: str) -> str:
        """Determine product (CP/MNP) from event"""
        headers = {"Authorization": f"Bearer {self.get_token()}"}
        data = {
            "work_item_type": 0,
            "work_item_channel": 2,
            "work_item_operator": "yuxuan.li",
            "page_num": 1,
            "page_size": 5,
            "search_meta": {"event_id": event_id},
        }

        try:
            response = requests.post(
                "https://ess.momenta.works/api/v1/event/case/_list",
                headers=headers,
                json=data,
            )
            response.raise_for_status()
            work_items = response.json().get("work_item", [])

            if work_items:
                work_item_id = work_items[0].get("work_item_id")
                cases = self.work_item_tool.query_work_items_v2(
                    project_key="65d6c20802cf2c99dcba17b9",
                    work_item_type_key="65fab2c09169e28c18e6ff7f",
                    work_item_ids=[work_item_id],
                )

                for case in cases:
                    for field in case.get("fields", []):
                        if field.get("field_key") == "field_5e48d0":
                            label = field.get("field_value", {}).get("label", "")
                            if label == "MNP":
                                return "MNP"
        except Exception as e:
            print(f"Failed to determine product: {e}")

        return "CP"

    def _query_vehicle_from_simulation(self, vehicle_type: str) -> str:
        """Query vehicle box ID from simulation platform with caching"""
        # Check cache first
        if vehicle_type in self._vehicle_cache:
            return self._vehicle_cache[vehicle_type]

        headers = {"Authorization": f"Bearer {self.get_token()}"}
        url = f"https://simulation.momenta.works/portal/car_info_list?host_name={vehicle_type}"

        try:
            response = requests.get(url, headers=headers, timeout=API_TIMEOUT)
            response.raise_for_status()
            cars = response.json()
            if cars:
                device_box_id = cars[0].get("device_box_id", DEFAULT_VEHICLE)
                # Cache the result
                self._vehicle_cache[vehicle_type] = device_box_id
                return device_box_id
        except Exception as e:
            print(f"Failed to query vehicle: {e}")

        # Cache default vehicle for this type to avoid repeated failures
        self._vehicle_cache[vehicle_type] = DEFAULT_VEHICLE
        return DEFAULT_VEHICLE

    def get_artifact_link(self, package_name: str) -> str:
        """Resolve package name to artifact download link"""
        if package_name.startswith("https://"):
            return package_name

        headers = {"Authorization": f"Bearer {self.get_token()}"}

        # Search for package
        search_url = f"https://ep.momenta.works/backend/package-meta/api/v1/metas/list?page_num=1&page_size=20&package_names={package_name}&include_delete=true"
        search_res = requests.get(search_url, headers=headers, timeout=API_TIMEOUT)
        search_res.raise_for_status()

        data = search_res.json()
        build_id = None

        for meta in data["data"]["data"][0]["meta"]:
            if meta["key"] == "build_id":
                build_id = meta["value"]
                break

        if not build_id:
            raise ValueError(f"Build ID not found for package {package_name}")

        # Get artifact link
        info_url = f"https://ep.momenta.works/backend/package-meta/api/v1/meta/info?package_name={package_name}&build_id={build_id}&include_delete=true"
        info_res = requests.get(info_url, headers=headers, timeout=API_TIMEOUT)
        info_res.raise_for_status()

        for meta in info_res.json()["data"]["meta"]:
            if meta["key"] == "all_tar_url":
                return meta["value"]

        raise ValueError(f"Artifact link not found for package {package_name}")

    def get_mf_branch_and_commit(self, package_name: str) -> Tuple[str, str]:
        """
        Get MF branch and commit ID from package metadata
        
        Args:
            package_name: MF package name
            
        Returns:
            (mf_branch, mf_commit_id)
        """
        headers = {"Authorization": f"Bearer {self.get_token()}"}
        
        # Search for package
        search_url = f"https://ep.momenta.works/backend/package-meta/api/v1/metas/list?page_num=1&page_size=20&package_names={package_name}&include_delete=true"
        search_res = requests.get(search_url, headers=headers, timeout=API_TIMEOUT)
        search_res.raise_for_status()

        data = search_res.json()
        build_id = None

        for meta in data["data"]["data"][0]["meta"]:
            if meta["key"] == "build_id":
                build_id = meta["value"]
                break

        if not build_id:
            raise ValueError(f"Build ID not found for package {package_name}")

        # Get detailed info
        info_url = f"https://ep.momenta.works/backend/package-meta/api/v1/meta/info?package_name={package_name}&build_id={build_id}&include_delete=true"
        info_res = requests.get(info_url, headers=headers, timeout=API_TIMEOUT)
        info_res.raise_for_status()
        
        mf_branch = None
        mf_commit = None
        
        for meta in info_res.json()["data"]["meta"]:
            if meta.get("key") == "module_list_branch":
                mf_branch = meta.get("value")
            if meta.get("key") == "module_list_commit_id":
                mf_commit = meta.get("value")
        
        if not mf_branch or not mf_commit:
            raise ValueError(f"MF branch/commit not found for package {package_name}")
        
        return mf_branch, mf_commit

    def create_version_test_task(
        self,
        package_name: str,
        scenario_id: str,
        scenario_name: str,
        user: str,
        vehicle_name: str,
        product: str = "CP",
        fst_path: str = "CP_FST/CP",
        use_ci_build: bool = True,
    ) -> str:
        """
        Create Version Test task with CI BUILD support (CP 产品仿真复现最佳实践)

        Args:
            package_name: MF package name or DevCar package name
            scenario_id: Scenario set ID
            scenario_name: Scenario set name
            user: Task owner
            vehicle_name: Vehicle box ID
            product: Product type (CP/MNP)
            fst_path: FST tree path
            use_ci_build: Whether to use CI BUILD (default: True for MF packages)

        Returns:
            vt_id: Version Test task ID
        """
        from pyevents import event_library_services as els

        print("   [1/4] Loading product config...")
        # Get product config for profile and which_car
        prod_config = self.products.get(product, {})
        profile = prod_config.get("profile", "default")
        which_car = prod_config.get("which_car", vehicle_name)
        checker_image = prod_config.get("checker_image", CHECKER_IMAGE)

        print("   [2/4] Getting scenario set details...")
        # Get scenario set details
        scenario_info = els.get_scenario_set_details(id=scenario_id)
        repo = scenario_info['repos']
        name = scenario_info['name']

        print("   [3/4] Building VT params...")
        # Build VT params
        vt_params = {
            "owner": user,
            "source": "cp_system_rd",
            "name": f"SimICT_{product}_{which_car}_{time.strftime('%Y%m%d%H%M%S')}",
            "priority": DEFAULT_PRIORITY,
            "product": product,
            "rerun_checker": False,
            "remark": f"CP产品仿真复现 (CI BUILD) - Package: {package_name}",
            "version_spec": {},
            "batches": [
                {
                    "name": f"Batch_{name}",
                    "ess_tree": {
                        "version": "master",
                        "version_type": "branch",
                        "repos": repo,
                        "branch": "master",
                        "nodes": [],
                        "virtual_nodes": [
                            {
                                "tree_path": {
                                    "path": fst_path,
                                    "profile": profile
                                },
                                "scenario_sets": [
                                    {
                                        "repos": repo,
                                        "name": name,
                                        "id": scenario_id
                                    }
                                ],
                                "checkers": DISABLED_CHECKERS
                            }
                        ]
                    },
                    "overrides": {},
                    "which_car": which_car,
                    "checker_image": checker_image
                }
            ]
        }
        
        # Add CI BUILD config if needed
        if use_ci_build:
            try:
                mf_branch, mf_commit = self.get_mf_branch_and_commit(package_name)
                
                # 优先使用产品自身的 ci_build_config, fallback 到全局默认
                product_ci_config = prod_config.get("ci_build_config")
                ci_config = (product_ci_config if product_ci_config else DEFAULT_CI_BUILD_CONFIG).copy()
                ci_config["mf_commit_id"] = mf_commit
                ci_config["mf_system_branch"] = mf_branch
                
                vt_params["version_spec"]["simulation"] = {
                    "Devcar": {
                        "ci_build_config": ci_config
                    }
                }
                
                print(f"✅ CI BUILD 配置已添加:")
                print(f"   MF Branch: {mf_branch}")
                print(f"   MF Commit: {mf_commit}")
                
            except Exception as e:
                print(f"⚠️  无法获取 MF 包元数据，将使用直接产物链接: {e}")
                use_ci_build = False
        
        # Submit VT task
        print("   [4/4] Submitting VT task...")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.get_token()}",
        }

        response = requests.post(
            "https://simulation.momenta.works/portal/api/v1/version_test/create",
            headers=headers,
            json=vt_params,
            timeout=API_TIMEOUT,
        )
        response.raise_for_status()

        vt_id = response.json()["_id"]
        return vt_id

    def create_vt_from_template(
        self,
        template_link: str,
        scenario_id: str,
        scenario_name: str,
        user: str,
        vehicle_name: str,
        product: str = "NP",
        fst_path: str = "HNP-FST",
        package_version: Optional[str] = None,
        conservative_mode: bool = False,
    ) -> str:
        """
        Create Version Test task by copying from template (NP/L3 产品仿真模式)

        Args:
            template_link: Template VT task URL
            scenario_id: Scenario set ID
            scenario_name: Scenario set name
            user: Task owner
            vehicle_name: Vehicle box ID
            product: Product type (NP/L3)
            fst_path: FST tree path
            package_version: Override package version (optional)
            conservative_mode: Enable conservative driving style

        Returns:
            vt_id: Version Test task ID
        """
        from pyevents import event_library_services as els

        print("   [1/5] Loading product config...")
        # Get product config for profile and which_car
        prod_config = self.products.get(product, {})
        profile = prod_config.get("profile", "default")
        which_car = prod_config.get("which_car", vehicle_name)
        checker_image = prod_config.get("checker_image", CHECKER_IMAGE)

        print("   [2/5] Getting scenario set details...")
        # Get scenario set details
        scenario_info = els.get_scenario_set_details(id=scenario_id)
        repo = scenario_info['repos']
        name = scenario_info['name']
        
        print("   [3/5] Fetching template VT config...")
        # Get template VT config
        vt_id = template_link.split("/")[-1]
        headers = {"Authorization": f"Bearer {self.get_token()}"}
        response = requests.get(
            f"https://simulation.momenta.works/portal/api/v1/version_test/{vt_id}",
            headers=headers,
            timeout=API_TIMEOUT,
        )
        response.raise_for_status()
        vt_params = response.json()
        
        print("   [4/5] Modifying template params...")
        # Clean up IDs
        del vt_params["_id"]
        del vt_params["version_spec"]["_id"]
        if "artifact_links" in vt_params:
            del vt_params["artifact_links"]

        # Update metadata
        vt_params["owner"] = user
        vt_params["source"] = "cp_system_rd"
        vt_params["name"] = f"SimICT_{product}_{which_car}_{time.strftime('%Y%m%d%H%M%S')}"
        vt_params["rerun_checker"] = False
        vt_params["remark"] = f"[Copied from {template_link}] - L3/NP 产品仿真"

        # Clean up empty package_name fields in version_spec to avoid API errors
        # (camera parameters query fails with empty package_name)
        try:
            for stage in ["simulation", "refresh"]:
                if stage in vt_params["version_spec"]:
                    for pkg_type in list(vt_params["version_spec"][stage].keys()):
                        pkg_spec = vt_params["version_spec"][stage][pkg_type]
                        if isinstance(pkg_spec, dict):
                            # Remove empty package_name fields
                            if pkg_spec.get("package_name") == "":
                                del pkg_spec["package_name"]
                                print(f"   ⚠️  已清理空 package_name 字段: {stage}.{pkg_type}")
        except (KeyError, TypeError) as e:
            print(f"   ⚠️  清理 version_spec 时出错: {e}")

        # Override package version if provided
        if package_version:
            try:
                vt_params["version_spec"]["simulation"]["Devcar"]["package_version"] = package_version
                vt_params["version_spec"]["refresh"]["Devcar"]["package_version"] = package_version
                print(f"✅ 产物版本已覆盖: {package_version}")
            except KeyError:
                pass
        
        # Update batches with new scenario set
        for batch in vt_params["batches"]:
            # Clean batch IDs
            del batch["_id"]
            del batch["version_test_id"]
            
            # Update ESS tree
            batch["ess_tree"]["version"] = "master"
            batch["ess_tree"]["version_type"] = "branch"
            batch["ess_tree"]["repos"] = repo
            batch["ess_tree"]["branch"] = "master"
            batch["ess_tree"]["nodes"] = []
            
            # Update virtual nodes with new scenario
            batch["ess_tree"]["virtual_nodes"] = [{
                "tree_path": {
                    "path": fst_path,
                    "profile": profile
                },
                "scenario_sets": [{
                    "repos": repo,
                    "name": name,
                    "id": scenario_id
                }],
                "checkers": DISABLED_CHECKERS
            }]

            # Clear overrides (will be set below if needed)
            batch["overrides"] = {}
            
            # Update vehicle and checker
            batch["which_car"] = which_car
            if "checker_image" in batch:
                batch["checker_image"] = checker_image
            
            # Apply conservative mode if enabled
            if conservative_mode:
                stage_specs = batch.get("overrides", {}).get("stage_specs", {})
                
                # Update FPP stage with conservative driving style
                fpp_stage = stage_specs.get("FPP", {})
                fpp_args = fpp_stage.get("arguments", [])
                
                # Ensure conservative arguments
                base_args = [
                    "--open-loop-mode", "invalid",
                    "--dynamics", "model_based_dynamics",
                    "--driving-style", "conservative"
                ]
                
                # Merge with existing args (avoid duplicates)
                existing_keys = set()
                for i in range(0, len(fpp_args), 2):
                    if i + 1 < len(fpp_args):
                        existing_keys.add(fpp_args[i])
                
                for i in range(0, len(base_args), 2):
                    key = base_args[i]
                    val = base_args[i + 1]
                    if key not in existing_keys:
                        fpp_args.extend([key, val])
                
                fpp_stage["arguments"] = fpp_args
                fpp_stage["options"] = fpp_stage.get("options", {})
                fpp_stage["options"]["modules_in_loop"] = "planning,controller"
                
                stage_specs["FPP"] = fpp_stage
                
                # Update EBM stage
                ebm_stage = stage_specs.get("EBM", {})
                ebm_stage["options"] = ebm_stage.get("options", {})
                ebm_stage["options"].update({
                    "clear_refresh_outputs": "true",
                    "no-cache": "true",
                    "no-triton": "true"
                })
                stage_specs["EBM"] = ebm_stage
                
                batch["overrides"]["stage_specs"] = stage_specs
                
                print(f"   Conservative mode enabled")

        # Submit VT task
        print("   [5/5] Submitting VT task...")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.get_token()}",
        }

        response = requests.post(
            "https://simulation.momenta.works/portal/api/v1/version_test/create",
            headers=headers,
            json=vt_params,
            timeout=API_TIMEOUT,
        )
        response.raise_for_status()

        vt_id = response.json()["_id"]
        return vt_id

    def create_task_batch(self, user: str, mode: str) -> Tuple[str, str]:
        """
        Create task batch for organizing tasks

        Returns:
            (batch_id, batch_url)
        """
        batch_name_map = {
            "simulation": "Only Simulation仿真",
            "1x1": "SimICT 1x1仿真",
            "2x2": "SimICT 2x2仿真",
        }

        batch_name = batch_name_map.get(mode, "仿真")
        task_name = f"CP Bot代提_{user}_{time.strftime('%Y%m%d%H%M%S')}"

        payload = {
            "name": task_name,
            "owner": user,
            "remark": "",
            "source": "cp_system_rd",
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.get_token()}",
        }

        response = requests.post(
            "https://simulation.momenta.works/portal/api/v1/task/batch/register",
            headers=headers,
            json=payload,
            timeout=API_TIMEOUT,
        )
        response.raise_for_status()

        batch_id = response.json()["_id"]
        batch_url = (
            f"https://simulation.momenta.works/batch/{batch_id}?tab=task_list"
        )

        return batch_id, batch_url

    def create_simulation_task(
        self,
        artifact: str,
        scenario_id: str,
        scenario_name: str,
        user: str,
        batch_id: str,
        vehicle_name: str,
        mode: str = "simulation",
    ) -> str:
        """
        Create simulation task

        Args:
            mode: "simulation" | "1x1" | "2x2"

        Returns:
            task_id
        """
        artifact_link = self.get_artifact_link(artifact)

        params = {
            "batch_id": batch_id,
            "stage_specs": {},
            "checker_image": CHECKER_IMAGE,
            "checkers": DISABLED_CHECKERS,
            "scenario_set": {
                "repos": "CP_FST",
                "name": scenario_name,
                "id": scenario_id,
            },
            "generate_geo_viz": False,
            "landmark_mode": "DDLD",
            "name": f"SimICT_CP_{vehicle_name}_{time.strftime('%Y%m%d%H%M%S')}",
            "owner": user,
            "priority": DEFAULT_PRIORITY,
            "product": "CP",
            "remark": "",
            "task_source": "cp_system_rd",
            "arguments": [
                "--play-model-based-dynamics",
                "--play-prediction-msgs",
                "--cone-update",
                "position",
                "--ignore-original-planning-request",
                "off",
                "--init-auto-speed",
                "-1",
                "--lane-update",
                "position",
                "--navi-time-distance",
                "0",
                "--dynamics",
                "model_based_dynamics",
                "--consistency-mode",
                "false",
            ],
            "artifact_link": artifact_link,
            "version_set": {
                "modules": {
                    "calib-data": {
                        "image": "artifactory.momenta.works/docker-msd/simulation/calib-data:leo_mfbag-8af8eccc6-20250609-1129"
                    },
                    "slp": {
                        "image": "artifactory.momenta.works/docker-mpilot-highway-dev/slp:2025.06.09.11.45.leo-mfbag.8dd9a714"
                    },
                }
            },
            "modules_in_loop": "controller,planning",
            "type": "SLP2.0",
            "which_car": vehicle_name,
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.get_token()}",
        }

        response = requests.post(
            "https://simulation.momenta.works/portal/api/v1/task/create/pnc",
            headers=headers,
            json=params,
        )

        try:
            response.raise_for_status()
            return response.json()["id"]
        except Exception as e:
            print(f"⚠️  PNC 任务创建失败 (HTTP {response.status_code}): {response.text[:500]}")
            # Retry with default vehicle
            print(f"🔄 使用默认车辆 EP33L_AA60351 重试...")
            params["which_car"] = "EP33L_AA60351"
            response = requests.post(
                "https://simulation.momenta.works/portal/api/v1/task/create/pnc",
                headers=headers,
                json=params,
            )
            if response.status_code != 200:
                print(f"❌ 重试仍然失败 (HTTP {response.status_code}): {response.text[:500]}")
            response.raise_for_status()
            return response.json()["id"]


def main():
    """CLI entry point"""
    import argparse

    parser = argparse.ArgumentParser(
        description="Create SimICT simulation tasks (CP/NP products decoupled)",
        epilog="""
Examples:
  # MAINLINE 产品 (默认项目)
  simict-creator <mviz_link> --product IHC --user <union_id>
  
  # HARZ 项目
  simict-creator <mviz_link> --project harz --product CMSR --user <union_id>
  
  # CP 产品 (default 项目)
  simict-creator <mviz_link> <mf_package> --project default --product CP --user <union_id>
  
  # 自定义 ROI
  simict-creator <mviz_link> --product IHC --user <union_id> --roi-start 5 --roi-end 20
        """
    )
    parser.add_argument("bag_input", help="Bag name, event ID, Mviz link, or EQL query")
    parser.add_argument("artifact1", nargs="?", help="Primary artifact (MF package name, DevCar package, or URL) - optional for NP with template")
    parser.add_argument("--artifact2", help="Secondary artifact for 2x2 mode")
    parser.add_argument("--mode", choices=["simulation", "1x1", "2x2", "vt"], default="vt",
                        help="Simulation mode (default: vt)")
    parser.add_argument("--project", default="default",
                        help="Project name from config file (default: default)")
    parser.add_argument("--config", default=None,
                        help="Path to product_configs.yaml (auto-detect if not specified)")
    parser.add_argument("--product", default=None,
                        help="Product type (auto-detect if not specified)")
    parser.add_argument("--vehicle", help="Override vehicle name")
    parser.add_argument("--user", default=DEFAULT_USER,
                        help=f"Feishu union ID (default: {DEFAULT_USER[:20]}...)")
    parser.add_argument("--use-ci-build", action="store_true", default=None,
                        help="Force CI BUILD mode (default: auto, based on product's default_method)")
    parser.add_argument("--no-ci-build", action="store_true", default=False,
                        help="Disable CI BUILD, use direct artifact link instead")
    parser.add_argument("--fst-path", default=None,
                        help="FST tree path (auto-detect from product if not specified)")
    parser.add_argument("--roi-start", type=float, default=None,
                        help="ROI start time (default: 10)")
    parser.add_argument("--roi-end", type=float, default=None,
                        help="ROI end time (default: 25)")
    parser.add_argument("--conservative", action="store_true",
                        help="Enable conservative driving mode (for NP/L3)")
    parser.add_argument("--em-mode", action="store_true",
                        help="Enable EM mode (use EM template for NP)")

    args = parser.parse_args()

    creator = SimICTTaskCreator(config_path=args.config, project=args.project)

    # Resolve user
    user = creator.resolve_user(args.user)

    # Create scenario set
    scenario_id, scenario_name, vehicle, product = creator.create_scenario_set(
        bag_input=args.bag_input,
        vehicle_name=args.vehicle,
        roi_start=args.roi_start,
        roi_end=args.roi_end,
        product=args.product,
    )

    print(f"\n📦 Scenario Set Created:")
    print(f"   ID: {scenario_id}")
    print(f"   Name: {scenario_name}")
    print(f"   Vehicle: {vehicle}")
    print(f"   Product: {product}")
    print()

    # Get product config
    prod_config = creator.products.get(product)
    if not prod_config:
        available = ', '.join(creator.products.keys())
        print(f"❌ Error: Product '{product}' not found in project '{creator.project}'")
        print(f"   Available products: {available}")
        sys.exit(1)
    
    fst_path = args.fst_path or prod_config["fst_path"]
    default_method = prod_config["default_method"]

    # Resolve --use-ci-build / --no-ci-build → final boolean
    # 优先级: --no-ci-build > --use-ci-build > 智能判断(有artifact1用CI BUILD, 无artifact1用template)
    has_ci_config = "ci_build_config" in prod_config
    has_template = prod_config.get("template_task") is not None

    if args.no_ci_build:
        # 用户显式禁用 CI BUILD
        args.use_ci_build = False
    elif args.use_ci_build is True:
        # 用户显式启用 CI BUILD
        args.use_ci_build = True
    else:
        # 智能判断: 如果提供了 artifact1 且产品支持 CI BUILD → 使用 CI BUILD
        #           否则使用 template (如果可用)
        if args.artifact1 and has_ci_config:
            args.use_ci_build = True
        else:
            args.use_ci_build = False

    # Choose creation method based on product and mode
    use_ci_build_route = args.use_ci_build and has_ci_config

    if args.mode == "vt":
        if use_ci_build_route:
            # CI BUILD 模式 (有 artifact1 + 支持 CI BUILD)
            if not args.artifact1:
                print("❌ Error: artifact1 (MF package) is required for CI BUILD mode")
                print(f"💡 Tip: 如果想使用 Template 模式，请不要提供 artifact1 参数")
                sys.exit(1)
            
            print("🚀 创建 Version Test 任务 (CI BUILD 模式)")
            print(f"   产品: {product}")
            print(f"   FST Path: {fst_path}")
            print(f"   使用 CI BUILD: {args.use_ci_build}")
            print()
            
            vt_id = creator.create_version_test_task(
                package_name=args.artifact1,
                scenario_id=scenario_id,
                scenario_name=scenario_name,
                user=user,
                vehicle_name=vehicle,
                product=product,
                fst_path=fst_path,
                use_ci_build=args.use_ci_build,
            )
            
            vt_url = f"https://simulation.momenta.works/experiment/version_test/{vt_id}"
            
            # 获取任务详细信息用于显示
            prod_config = creator.products.get(product, {})
            checker_image = prod_config.get("checker_image", CHECKER_IMAGE)
            which_car = prod_config.get("which_car", vehicle)
            
            # 获取 checkers 信息
            checkers = DISABLED_CHECKERS

            print("\n" + "=" * 80)
            print("✅ Version Test 任务创建成功 (CI BUILD)！")
            print("=" * 80)
            print(f"\n📋 任务详情")
            print(f"- **产品**: {product} ({fst_path})")
            if args.roi_start is not None or args.roi_end is not None:
                print(f"- **ROI**: {args.roi_start or 10}s - {args.roi_end or 25}s")
            print(f"- **车辆**: {vehicle}")
            print(f"- **产物包**: {args.artifact1}")
            print(f"- **Scenario Set ID**: {scenario_id}")
            print()
            print(f"🔗 **任务链接**: {vt_url}")
            print()
            print(f"🛠️  **Checker 配置**")
            print(f"- **Checker Image**: `{checker_image}`")
            print(f"- **Vehicle Type**: `{which_car}`")
            print(f"- **Checkers**: `{', '.join(checkers)}`")
            print()
            print("⏱️  预计耗时:")
            print("   - DevCar CI BUILD: 20-40 分钟")
            print("   - SLP2.0 仿真: 15-30 分钟")
            print("   - 总计: ~35-70 分钟")
            print()
            print("💡 提示:")
            print(f"   - 模式: {product} 产品仿真复现 (Version Test)")
            print(f"   - 优先级: High ({DEFAULT_PRIORITY})")
            print("   - 使用 CI BUILD 从 MF 包动态生成 DevCar 产物")
            print()
            print("⚠️  **重要**: 任务创建后需在仿真平台手动提升优先级 (会消耗 quota),")
            print("   否则任务将一直处于 suspend 状态, 不会进入运行队列。")

        elif has_template:
            # Template 复制模式 (没有 artifact1 时自动使用)
            template_link = prod_config.get("template_task_em") if args.em_mode else prod_config.get("template_task")

            if not template_link or ("[" in template_link and "]" in template_link):
                print(f"❌ Error: 产品 {product} 未配置有效的 template_task")
                if has_ci_config:
                    print(f"💡 Tip: 该产品支持 CI BUILD 模式，请提供 MF 包作为第二个参数")
                    print(f"   示例: simict_creator.py <events> <mf_package> --product {product}")
                sys.exit(1)
            
            print("🚀 创建 Version Test 任务 (Template 复制模式)")
            print(f"   产品: {product}")
            print(f"   FST Path: {fst_path}")
            print(f"   Template: {template_link}")
            print(f"   Conservative: {args.conservative}")
            print(f"   EM Mode: {args.em_mode}")
            print()
            
            vt_id = creator.create_vt_from_template(
                template_link=template_link,
                scenario_id=scenario_id,
                scenario_name=scenario_name,
                user=user,
                vehicle_name=vehicle,
                product=product,
                fst_path=fst_path,
                package_version=args.artifact1,  # Optional override
                conservative_mode=args.conservative,
            )
            
            vt_url = f"https://simulation.momenta.works/experiment/version_test/{vt_id}"
            
            # 获取任务详细信息用于显示
            prod_config = creator.products.get(product, {})
            checker_image = prod_config.get("checker_image", CHECKER_IMAGE)
            which_car = prod_config.get("which_car", vehicle)
            
            # 获取 checkers 信息
            checkers = DISABLED_CHECKERS

            print("\n" + "=" * 80)
            print("✅ Version Test 任务创建成功 (Template 模式)！")
            print("=" * 80)
            print(f"\n📋 任务详情")
            print(f"- **产品**: {product} ({fst_path})")
            if args.roi_start is not None or args.roi_end is not None:
                print(f"- **ROI**: {args.roi_start or 10}s - {args.roi_end or 25}s")
            print(f"- **车辆**: {vehicle}")
            print(f"- **Scenario Set ID**: {scenario_id}")
            print()
            print(f"🔗 **任务链接**: {vt_url}")
            print()
            print(f"🛠️  **Checker 配置**")
            print(f"- **Checker Image**: `{checker_image}`")
            print(f"- **Vehicle Type**: `{which_car}`")
            print(f"- **Checkers**: `{', '.join(checkers)}`")
            print()
            print(f"⏱️  预计耗时:")
            if args.use_ci_build:
                print("   - DevCar CI BUILD: 20-40 分钟")
            print("   - SLP2.0 仿真: 15-30 分钟")
            print(f"   - 总计: ~{'35-70' if args.use_ci_build else '15-30'} 分钟")
            print()
            print("💡 提示:")
            print(f"   - 模式: {product} 产品仿真复现 (Version Test)")
            print(f"   - 优先级: High ({DEFAULT_PRIORITY})")
            print(f"   - Template: {template_link.split('/')[-1]}")
            print()
            print("⚠️  **重要**: 任务创建后需在仿真平台手动提升优先级 (会消耗 quota),")
            print("   否则任务将一直处于 suspend 状态, 不会进入运行队列。")

        else:
            # 既没有 template 也没有提供 artifact1
            print(f"❌ Error: 产品 {product} 无法创建任务")
            print(f"\n该产品需要以下之一:")
            if has_ci_config:
                print(f"  1. 提供 MF 包使用 CI BUILD 模式:")
                print(f"     simict_creator.py <events> <mf_package> --product {product}")
            if has_template:
                print(f"  2. 使用 Template 模式 (当前 template 未配置或无效)")
            if not has_ci_config and not has_template:
                print(f"  - 该产品未配置 CI BUILD 或 Template 支持")
                print(f"  - 请联系产品负责人配置 product_configs.yaml")
            sys.exit(1)

    else:
        # 传统 PNC Task 模式
        # Create task batch
        batch_id, batch_url = creator.create_task_batch(user, args.mode)

        # Create tasks based on mode
        if args.mode == "simulation":
            task_id = creator.create_simulation_task(
                args.artifact1, scenario_id, scenario_name, user, batch_id, vehicle, "simulation"
            )
            print(f"✅ Task created: https://simulation.momenta.works/task/{task_id}")

        elif args.mode == "1x1":
            task_id = creator.create_simulation_task(
                args.artifact1, scenario_id, scenario_name, user, batch_id, vehicle, "1x1"
            )
            print(f"✅ 1x1 Task created: https://simulation.momenta.works/task/{task_id}")

        elif args.mode == "2x2":
            if not args.artifact2:
                parser.error("--artifact2 is required for 2x2 mode")

            # Create 4 tasks: A刷B, B刷A, A刷A, B刷B
            tasks = []
            for art1, art2 in [
                (args.artifact1, args.artifact2),
                (args.artifact2, args.artifact1),
                (args.artifact1, args.artifact1),
                (args.artifact2, args.artifact2),
            ]:
                task_id = creator.create_simulation_task(
                    art1, scenario_id, scenario_name, user, batch_id, vehicle, "2x2"
                )
                tasks.append(task_id)

            print(f"✅ 2x2 Tasks created: {len(tasks)} tasks")
            for i, tid in enumerate(tasks, 1):
                print(f"  {i}. https://simulation.momenta.works/task/{tid}")

        print(f"\n📦 Batch: {batch_url}")

        print()
        print("⚠️  **重要**: 任务创建后需在仿真平台手动提升优先级 (会消耗 quota),")
        print("   否则任务将一直处于 suspend 状态, 不会进入运行队列。")


if __name__ == "__main__":
    main()
