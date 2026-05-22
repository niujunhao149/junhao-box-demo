"""
Fill Sim Results 服务
包装 fill_sim_results.mjs，通过 subprocess 调用并实时上报进度
"""
import subprocess
import re
import threading
from pathlib import Path
from typing import Callable

SCRIPT_DIR = Path("C:/Users/junhao.niu/.claude/.agents/skills/case-notbug")
SCRIPT_PATH = SCRIPT_DIR / "fill_sim_results.mjs"


class FillSimService:
    """
    run() 同步执行（通过 asyncio.to_thread 调用）
    on_progress(progress: int, message: str) 全程回调
    返回 dict: {ok, fail, skip}
    """

    def run(self, batch_url: str, view_url: str, version: str, on_progress: Callable) -> dict:
        on_progress(5, "正在启动脚本...")

        cmd = ["node", str(SCRIPT_PATH), "--batch", batch_url, "--view", view_url, "--ver", version]

        proc = subprocess.Popen(
            cmd,
            cwd=str(SCRIPT_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,  # 合并 stderr 到 stdout
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

        result = {"ok": 0, "fail": 0, "skip": 0}
        all_lines = []

        # Step3 扫描进度解析用
        scan_total = None

        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            if not line:
                continue
            all_lines.append(line)

            # ── Step 1 ──────────────────────────────────
            if "Step 1: Fetch SimICT" in line:
                on_progress(10, "正在获取 SimICT batch 数据...")
            elif line.startswith("Batch:"):
                name = line.replace("Batch:", "").strip()
                on_progress(15, f"Batch: {name}")
            elif "event_run_ids:" in line:
                m = re.search(r"event_run_ids:\s*(\d+)", line)
                cnt = m.group(1) if m else "?"
                on_progress(25, f"找到 {cnt} 个 event_run_ids，正在查询详情...")
            elif line.startswith("Events:"):
                on_progress(30, line)

            # ── Step 2 ──────────────────────────────────
            elif "Step 2: Scroll view" in line:
                on_progress(35, "正在滚动飞书 Case 视图，加载全量 Case（约 20-60 秒）...")
            elif "View case IDs captured:" in line:
                m = re.search(r"(\d+)", line)
                cnt = m.group(1) if m else "?"
                on_progress(60, f"已捕获 {cnt} 个 Case ID")

            # ── Step 3 ──────────────────────────────────
            elif "Step 3: Match events" in line:
                on_progress(65, "正在匹配 Event → Case...")
            elif re.search(r"(\d+)/(\d+) scanned", line):
                m = re.search(r"(\d+)/(\d+) scanned.*matched:\s*(\d+)", line)
                if m:
                    cur, total, matched = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    scan_total = total
                    pct = 65 + int((cur / total) * 20) if total else 65
                    on_progress(pct, f"扫描 Case {cur}/{total}，已匹配 {matched} 个")
            elif line.startswith("Matched:"):
                on_progress(87, f"匹配完成 — {line}")

            # ── Step 4 ──────────────────────────────────
            elif "Step 4: Write results" in line:
                on_progress(90, "正在批量写入仿真结果到 Case...")
            elif re.match(r"\[\d+/\d+\]", line):
                # 每条写入进度，按比例更新 90-97%
                m = re.match(r"\[(\d+)/(\d+)\]", line)
                if m:
                    cur2, total2 = int(m.group(1)), int(m.group(2))
                    pct2 = 90 + int((cur2 / total2) * 7) if total2 else 90
                    icon = "✅" if "✅" in line else ("⚠️" if "⚠️" in line else ("⏭" if "⏭" in line else "❌"))
                    on_progress(pct2, f"写入 {cur2}/{total2} {icon}")

            # ── 完成行 ──────────────────────────────────
            elif "=== 完成 ===" in line:
                on_progress(97, "正在收尾...")
            elif re.match(r"写入成功:\s*\d+", line):
                m = re.match(r"写入成功:\s*(\d+)\s+失败:\s*(\d+)\s+跳过:\s*(\d+)", line)
                if m:
                    result["ok"] = int(m.group(1))
                    result["fail"] = int(m.group(2))
                    result["skip"] = int(m.group(3))

        proc.wait()

        if proc.returncode != 0:
            tail = "\n".join(all_lines[-20:])
            raise RuntimeError(f"脚本异常退出 (code={proc.returncode}):\n{tail}")

        on_progress(100, f"完成！写入成功 {result['ok']}，失败 {result['fail']}，跳过 {result['skip']}")
        return result
