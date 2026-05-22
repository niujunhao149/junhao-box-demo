"""
批量创建飞书 Case 服务
从 Mviz / ESS 链接提取 Event ID，调用 ess case create 批量创建
"""
import subprocess
import re
from typing import Callable, List
from urllib.parse import urlparse, parse_qs
from ..config import get_settings

ESS_PYTHON = r"C:\Users\junhao.niu\AppData\Local\Programs\Python\Python311\python.exe"


def extract_event_id(link: str) -> str | None:
    """从各种格式的链接中提取 Event ID"""
    link = link.strip()
    if not link:
        return None

    # ESS 链接: https://ess.momenta.works/events/detail?id=EVENT_ID
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", link)
    if m:
        return m.group(1)

    # MAF Mviz: .../open-ui/v1/event/EVENT_ID/bag/...
    m = re.search(r"/event/([a-zA-Z0-9_-]+)/", link)
    if m:
        return m.group(1)

    # 裸 Event ID（纯字母数字字符串，不含 /）
    if re.match(r"^[a-zA-Z0-9_-]{8,}$", link):
        return link

    return None


class CreateCaseService:
    """
    run() 同步执行（通过 asyncio.to_thread 调用）
    on_progress(progress, message) 全程回调
    返回 dict: {ok, fail, links: [jump_link, ...], summary}
    """

    def run(
        self,
        links: List[str],
        product: str,
        feishu_operator: str,
        feishu_email: str,
        on_progress: Callable,
    ) -> dict:
        # 提取 Event ID
        on_progress(5, "正在解析链接...")
        event_ids = []
        for link in links:
            eid = extract_event_id(link)
            if eid:
                event_ids.append(eid)

        if not event_ids:
            raise RuntimeError("未能从输入中提取到任何 Event ID，请检查链接格式")

        on_progress(10, f"解析完成，共 {len(event_ids)} 个 Event ID，开始创建 Case...")

        cmd = [
            ESS_PYTHON, "-m", "ess_dev",
            "case", "create",
            ",".join(event_ids),
            "--product", product,
        ]

        settings = get_settings()
        env_vars = {
            "ESS_USERNAME": settings.ESS_DEFAULT_USERNAME,
            "ESS_PASSWORD": settings.ESS_DEFAULT_PASSWORD,
            "FEISHU_OPERATOR": feishu_operator,
            "FEISHU_OPERATOR_EMAIL": feishu_email,
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }

        import os
        env = os.environ.copy()
        env.update(env_vars)

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )

        result = {"ok": 0, "fail": 0, "links": [], "summary": ""}
        all_lines = []
        total = len(event_ids)

        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            # 跳过空行和进度条覆写行
            if not line or "\r" in raw_line:
                continue
            all_lines.append(line)

            # 预取进度
            if "预取 Event 数据" in line:
                on_progress(15, "正在预取 Event 数据...")
            elif "完成" in line and "预取" not in line and "/" in line:
                m = re.search(r"完成\s*\((\d+)/(\d+)\)", line)
                if m:
                    done, total_cnt = int(m.group(1)), int(m.group(2))
                    pct = 15 + int((done / max(total_cnt, 1)) * 70)
                    on_progress(pct, f"正在创建 Case... ({done}/{total_cnt})")
            # 最终汇总
            elif "创建完成:" in line:
                m = re.search(r"创建完成:\s*(\d+)\s*成功,\s*(\d+)\s*失败", line)
                if m:
                    result["ok"] = int(m.group(1))
                    result["fail"] = int(m.group(2))
                    result["summary"] = line.strip()
                    on_progress(95, f"创建完成：{m.group(1)} 成功，{m.group(2)} 失败")
            # 提取 Case 跳转链接
            elif "Case:" in line:
                m = re.search(r"Case:\s*(https://\S+)", line)
                if m:
                    result["links"].append(m.group(1))

        proc.wait()

        if proc.returncode != 0 and result["ok"] == 0:
            tail = "\n".join(all_lines[-20:])
            raise RuntimeError(f"Case 创建失败 (code={proc.returncode}):\n{tail}")

        if not result["summary"]:
            result["summary"] = f"完成：{result['ok']} 成功，{result['fail']} 失败"

        on_progress(100, result["summary"])
        return result
