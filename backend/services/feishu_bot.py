"""飞书机器人服务"""
import httpx
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta
from typing import Dict, Optional, Tuple
from ..config import get_settings
from ..utils.logger import get_logger

logger = get_logger(__name__)
settings = get_settings()


class FeishuBot:
    """飞书机器人客户端"""

    def __init__(self):
        self.app_id = settings.FEISHU_APP_ID
        self.app_secret = settings.FEISHU_APP_SECRET
        self._access_token = None
        self._token_expire_time = None

    async def get_tenant_access_token(self) -> str:
        """获取 tenant_access_token（应用级别 token）"""
        # 如果 token 还未过期，直接返回
        if self._access_token and self._token_expire_time:
            if datetime.now() < self._token_expire_time:
                return self._access_token

        # 获取新 token
        url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=10
            )
            data = response.json()

            if data.get("code") != 0:
                raise Exception(f"获取 token 失败: {data.get('msg')}")

            self._access_token = data["tenant_access_token"]
            # token 有效期 2 小时，提前 5 分钟刷新
            expire_seconds = data.get("expire", 7200) - 300
            self._token_expire_time = datetime.now() + timedelta(seconds=expire_seconds)

            logger.info(f"Feishu bot token refreshed, expire in {expire_seconds}s")
            return self._access_token

    async def send_text_message(self, receive_id: str, text: str, receive_id_type: str = "chat_id") -> Dict:
        """
        发送文本消息

        Args:
            receive_id: 接收者 ID（user_id/chat_id/open_id）
            text: 消息文本
            receive_id_type: ID 类型（user_id/chat_id/open_id）
        """
        token = await self.get_tenant_access_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        payload = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": text})
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers=headers,
                params={"receive_id_type": receive_id_type},
                json=payload,
                timeout=10
            )
            return response.json()

    async def send_card_message(
        self,
        receive_id: str,
        card_content: Dict,
        receive_id_type: str = "chat_id"
    ) -> Dict:
        """
        发送卡片消息

        Args:
            receive_id: 接收者 ID
            card_content: 卡片内容（飞书卡片 JSON）
            receive_id_type: ID 类型
        """
        token = await self.get_tenant_access_token()
        url = "https://open.feishu.cn/open-apis/im/v1/messages"

        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json"
        }

        payload = {
            "receive_id": receive_id,
            "msg_type": "interactive",
            "content": json.dumps(card_content)
        }

        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                headers=headers,
                params={"receive_id_type": receive_id_type},
                json=payload,
                timeout=10
            )
            return response.json()

    def parse_user_input(self, text: str) -> Optional[Tuple[str, str, str]]:
        """
        解析用户输入

        支持格式：
        - "IP5PM-4032 今天"
        - "IP5PM-4032 昨天"
        - "IP5PM-4032 最近3天"
        - "IP5PM-4032 2026-03-20 2026-03-23"

        Returns:
            (车牌号, 开始时间, 结束时间) 或 None
        """
        text = text.strip()

        # 提取车牌号（第一个非空白词）
        parts = text.split()
        if len(parts) < 2:
            return None

        plate = parts[0]
        time_part = " ".join(parts[1:])

        now = datetime.now()

        # 解析时间范围
        if "今天" in time_part or "今日" in time_part:
            start = now.replace(hour=0, minute=0, second=0)
            end = now.replace(hour=23, minute=59, second=59)
        elif "昨天" in time_part or "昨日" in time_part:
            yesterday = now - timedelta(days=1)
            start = yesterday.replace(hour=0, minute=0, second=0)
            end = yesterday.replace(hour=23, minute=59, second=59)
        elif "最近3天" in time_part or "近3天" in time_part:
            start = (now - timedelta(days=2)).replace(hour=0, minute=0, second=0)
            end = now.replace(hour=23, minute=59, second=59)
        elif "最近7天" in time_part or "近7天" in time_part or "本周" in time_part:
            start = (now - timedelta(days=6)).replace(hour=0, minute=0, second=0)
            end = now.replace(hour=23, minute=59, second=59)
        else:
            # 尝试解析自定义日期
            date_pattern = r"(\d{4}-\d{2}-\d{2})\s+(\d{4}-\d{2}-\d{2})"
            match = re.search(date_pattern, time_part)
            if match:
                start_date_str = match.group(1)
                end_date_str = match.group(2)
                start = datetime.strptime(f"{start_date_str} 00:00:00", "%Y-%m-%d %H:%M:%S")
                end = datetime.strptime(f"{end_date_str} 23:59:59", "%Y-%m-%d %H:%M:%S")
            else:
                return None

        start_str = start.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end.strftime("%Y-%m-%d %H:%M:%S")

        return (plate, start_str, end_str)

    def create_welcome_card(self) -> Dict:
        """创建欢迎卡片（引导用户输入）"""
        return {
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "**📋 请按以下格式提供信息：**\n\n🚗 **车牌号 + 时间范围**\n\n支持的时间范围：\n• 今天\n• 昨天\n• 最近3天\n• 最近7天 / 本周\n• 自定义：YYYY-MM-DD YYYY-MM-DD\n\n**💡 示例：**\n```\nIP5PM-4032 今天\n```\n```\nIP5PM-4032 2026-03-20 2026-03-23\n```"
                    }
                }
            ],
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "🚗 ESS 路测数据自动整理"
                },
                "template": "blue"
            }
        }

    def create_processing_card(self, plate: str, start_time: str, end_time: str) -> Dict:
        """创建处理中卡片"""
        start_dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
        end_dt = datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S")

        return {
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"⏳ **正在整理路测数据...**\n\n🚗 车牌：`{plate}`\n📅 时间：`{start_dt.strftime('%Y-%m-%d %H:%M')} ~ {end_dt.strftime('%Y-%m-%d %H:%M')}`\n\n预计耗时：10-60 秒"
                    }
                }
            ],
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "处理中"
                },
                "template": "orange"
            }
        }

    def create_result_card(
        self,
        feishu_url: str,
        ess_share_url: str,
        event_count: int,
        time_span: str,
        duration: str
    ) -> Dict:
        """创建结果卡片"""
        return {
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"✅ **整理完成！共找到 {event_count} 个事件**\n"
                    }
                },
                {
                    "tag": "hr"
                },
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**📄 飞书文档**\n[点击查看]({feishu_url})"
                            }
                        },
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**🔍 ESS 搜索**\n[点击查看]({ess_share_url})"
                            }
                        }
                    ]
                },
                {
                    "tag": "hr"
                },
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**事件数量**\n{event_count}"
                            }
                        },
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**时间跨度**\n{time_span}"
                            }
                        },
                        {
                            "is_short": True,
                            "text": {
                                "tag": "lark_md",
                                "content": f"**处理耗时**\n{duration}"
                            }
                        }
                    ]
                }
            ],
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "✅ 整理完成"
                },
                "template": "green"
            }
        }

    def create_error_card(self, error_message: str) -> Dict:
        """创建错误卡片"""
        return {
            "config": {"wide_screen_mode": True},
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": f"❌ **任务失败**\n\n{error_message}"
                    }
                }
            ],
            "header": {
                "title": {
                    "tag": "plain_text",
                    "content": "任务失败"
                },
                "template": "red"
            }
        }


# 全局单例
_bot_instance = None


def get_feishu_bot() -> FeishuBot:
    """获取飞书机器人单例"""
    global _bot_instance
    if _bot_instance is None:
        _bot_instance = FeishuBot()
    return _bot_instance
