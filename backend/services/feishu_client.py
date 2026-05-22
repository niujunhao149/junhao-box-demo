"""飞书文档创建客户端"""
import sys
import json
import time
import httpx
from typing import List, Dict
from datetime import datetime
from pathlib import Path
from feishu_sync.api import ctx_current_token
from feishu_sync.retoken import get_access_token
from feishu_sync.api_write import (
    feishu_auth_header,
    convert_markdown_to_blocks,
    create_descendant_blocks,
    _patch_table_blocks
)
from feishu_sync.edit_arena import _strip_table_merge_info
from ..utils.logger import get_logger
from .text_cleaner import clean_description

logger = get_logger(__name__)

# 在模块加载时给 get_access_token 打补丁，添加 invalid_grant 重试逻辑
# 原因：飞书 refresh_token 是一次性的，并发请求时可能被其他线程/进程先消耗，
# 导致当前请求拿到已撤销的 token。重试可以让 FileLock 机制生效后拿到新 token。
_original_get_access_token = get_access_token


def _get_access_token_with_retry(ttl_threshold: int = 30) -> tuple:
    for attempt in range(3):
        try:
            return _original_get_access_token(ttl_threshold)
        except RuntimeError as e:
            if "invalid_grant" in str(e) and attempt < 2:
                logger.warning(f"Feishu refresh token revoked (attempt {attempt+1}/3), retrying...")
                time.sleep(1)
            else:
                raise
    return None  # unreachable


import feishu_sync.retoken as _retoken_mod
_retoken_mod.get_access_token = _get_access_token_with_retry


def sync_token_from_cloud(cloud_url: str, sync_key: str):
    """从云端同步飞书 token 到本地文件，避免两边各自刷新导致 refresh_token 冲突。

    云端是唯一刷新 token 的地方，本地只读取云端的 token 文件。
    """
    try:
        r = httpx.get(
            f"{cloud_url.rstrip('/')}/api/internal/feishu-token",
            params={"key": sync_key},
            timeout=10,
        )
        if r.status_code != 200:
            logger.warning(f"Failed to sync token from cloud: HTTP {r.status_code}")
            return False

        data = r.json()
        token_dir = Path("~/.feishu").expanduser()
        token_dir.mkdir(parents=True, exist_ok=True)

        for name in ("access_token", "refresh_token"):
            if name in data:
                p = token_dir / f"{name}.json"
                p.write_text(json.dumps(data[name], indent=4, ensure_ascii=False), encoding="utf-8")

        logger.info("Feishu token synced from cloud successfully")
        return True
    except Exception as e:
        logger.warning(f"Failed to sync token from cloud: {e}")
        return False


class FeishuClient:
    """飞书文档创建客户端"""

    def __init__(self):
        # 配置 stdout 编码（Windows 兼容）
        if sys.platform == 'win32':
            sys.stdout.reconfigure(encoding='utf-8')

        # 尝试从云端同步 token（本地模式，避免两边各自刷新 refresh_token）
        from ..config import get_settings
        s = get_settings()
        if s.CLOUD_FEISHU_URL:
            sync_token_from_cloud(s.CLOUD_FEISHU_URL, s.FEISHU_TOKEN_SYNC_KEY)

        # 获取飞书 access token
        token_str, _ = get_access_token()
        ctx_current_token.set(token_str)
        logger.info("Feishu token initialized")

    def create_document(
        self,
        plate: str,
        start_time: str,
        end_time: str,
        share_url: str,
        package_version: str,
        events: List[Dict]
    ) -> str:
        """
        创建飞书路测数据整理文档

        Args:
            plate: 车牌号
            start_time: 开始时间
            end_time: 结束时间
            share_url: ESS 搜索结果链接
            package_version: 测试包版本
            events: 事件列表

        Returns:
            飞书文档 URL
        """
        # 生成文档标题
        title = self._generate_title(start_time, end_time, plate)

        # 创建文档（在团队共享文件夹）
        # folder_token: G9ekfXI97lIM1Xd9MDdc2lJKn58 - "路测数据整理"共享文件夹
        r = httpx.post(
            "https://open.feishu.cn/open-apis/docx/v1/documents",
            headers=feishu_auth_header(),
            json={
                "title": title,
                "folder_token": "G9ekfXI97lIM1Xd9MDdc2lJKn58"
            },
            timeout=15,
        )
        assert r.status_code == 200 and r.json()["code"] == 0, f"创建文档失败: {r.text}"

        doc_id = r.json()["data"]["document"]["document_id"]
        doc_url = f"https://momenta.feishu.cn/docx/{doc_id}"
        logger.info(f"Created Feishu document: {doc_url}")

        # 插入元数据
        metadata_md = f"""数据链接：[ESS 搜索结果]({share_url})

牌照号：{plate}

时间：{start_time} ～ {end_time}

测试包版本：{package_version or "未知"}

# 数据整理"""

        meta_conv = convert_markdown_to_blocks(metadata_md)
        _patch_table_blocks(meta_conv["descendants"])
        create_descendant_blocks(
            doc_id, doc_id,
            meta_conv["children_id"],
            meta_conv["descendants"],
            index=-1
        )
        logger.info("Metadata inserted")

        # 插入表格
        self._insert_table(doc_id, events)
        logger.info(f"Table inserted with {len(events)} rows")

        return doc_url

    def _generate_title(self, start_time: str, end_time: str, plate: str) -> str:
        """
        生成文档标题

        格式: "日期_车牌_时间段_路测数据整理"
        同一天: "0323_IP5PM-4032_09:00-11:30_路测数据整理"
        跨天: "0301-0311_IP5PM-4032_09:00-11:30_路测数据整理"
        """
        start = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
        end = datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S")

        start_date = start.strftime("%m%d")
        end_date = end.strftime("%m%d")
        start_hm = start.strftime("%H:%M")  # 带冒号
        end_hm = end.strftime("%H:%M")      # 带冒号

        if start_date == end_date:
            return f"{start_date}_{plate}_{start_hm}-{end_hm}_路测数据整理"
        else:
            return f"{start_date}-{end_date}_{plate}_{start_hm}-{end_hm}_路测数据整理"

    def _insert_table(self, doc_id: str, events: List[Dict]):
        """
        插入平铺表格

        重要：不使用 edit_page draft 模式（对表格不稳定）
        直接用块 API 操作
        """
        rows = [
            "| 序 | 详细描述 | Event Name | MViz链接 |",
            "| --- | --- | --- | ---"
        ]

        for i, ev in enumerate(events, 1):
            # 清洗描述并转义管道符
            desc = clean_description(ev['description']).replace('|', '｜')
            ess_link = f"[{ev['name']}]({ev['ess_url']})"
            mviz_link = f"[MViz]({ev['mviz_url']})"
            rows.append(f"| {i} | {desc} | {ess_link} | {mviz_link} |")

        table_md = "\n".join(rows)
        conv = convert_markdown_to_blocks(table_md)
        children_ids = conv["children_id"]
        descendants = conv["descendants"]
        _patch_table_blocks(descendants)
        _strip_table_merge_info(descendants)

        create_descendant_blocks(doc_id, doc_id, children_ids, descendants, index=-1)
