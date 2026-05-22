"""
SimICT Task Creator 服务
包装 simict_creator.py，通过 subprocess 调用并实时上报进度
支持先创建 Scenario Set，再创建仿真任务
"""
import sys
import subprocess
import re
from pathlib import Path
from typing import Callable, Optional, List, Dict
from datetime import datetime

# simict_creator.py 与本文件同目录
SCRIPT_PATH = Path(__file__).parent / "simict_creator.py"
PYTHON = sys.executable


class SimictService:
    """
    run() 同步执行单个仿真任务（通过 asyncio.to_thread 调用）
    run_with_scenario_set() 先创建 Scenario Set，再创建仿真任务
    on_progress(progress: int, message: str) 全程回调
    返回 dict: {task_url, product, mode}
    """

    def run(
        self,
        input_link: str,
        product: str,
        on_progress: Callable,
        mf_package: Optional[str] = None,
        project: Optional[str] = None,
        roi_start: Optional[str] = None,
        roi_end: Optional[str] = None,
        conservative: bool = False,
        em_mode: bool = False,
        use_ci_build: bool = False,
    ) -> dict:
        on_progress(5, "正在准备任务参数...")

        # 构建命令行
        cmd = [PYTHON, str(SCRIPT_PATH), input_link]

        if mf_package:
            cmd.append(mf_package)

        cmd += ["--product", product]

        if project:
            cmd += ["--project", project]
        if roi_start:
            cmd += ["--roi-start", roi_start]
        if roi_end:
            cmd += ["--roi-end", roi_end]
        if conservative:
            cmd.append("--conservative")
        if em_mode:
            cmd.append("--em-mode")
        if use_ci_build:
            cmd.append("--use-ci-build")

        proc = subprocess.Popen(
            cmd,
            cwd=str(SCRIPT_PATH.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
        )

        result = {"task_url": "", "product": product, "mode": "vt"}
        all_lines = []

        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            if not line:
                continue
            all_lines.append(line)

            # 进度步骤解析 [N/5]
            m = re.match(r"\[(\d+)/(\d+)\]", line)
            if m:
                cur, total = int(m.group(1)), int(m.group(2))
                pct = 10 + int((cur / total) * 75)
                label = line[line.index("]") + 1:].strip()
                on_progress(pct, label)
                continue

            # 关键状态行
            if "首次运行" in line or "创建虚拟环境" in line:
                on_progress(8, "首次运行，正在初始化虚拟环境（约 1-3 分钟）...")
            elif "安装依赖" in line:
                on_progress(9, "正在安装依赖...")
            elif "🚀 创建 Version Test" in line or "🚀" in line:
                on_progress(15, line.replace("🚀", "").strip())
            elif "CI BUILD" in line and "创建" in line:
                on_progress(20, line)
            elif "等待" in line and ("CI BUILD" in line or "DevCar" in line):
                on_progress(50, line)
            elif "✅ Version Test 任务创建成功" in line:
                on_progress(95, "任务创建成功！")
            elif "✅ Task created" in line or "✅ 1x1 Task created" in line:
                on_progress(95, "任务创建成功！")
                m2 = re.search(r"https://simulation\S+", line)
                if m2:
                    result["task_url"] = m2.group(0)
                    result["mode"] = "pnc"

            # 提取任务链接
            elif "🔗 **任务链接**:" in line:
                url = line.split(":", 1)[1].strip()
                result["task_url"] = url
                result["mode"] = "vt"
            elif line.startswith("📦 Batch:"):
                url = line.split(":", 1)[1].strip()
                if not result["task_url"]:
                    result["task_url"] = url
                    result["mode"] = "batch"

        proc.wait()

        if proc.returncode != 0:
            tail = "\n".join(all_lines[-30:])
            raise RuntimeError(f"脚本异常退出 (code={proc.returncode}):\n{tail}")

        if not result["task_url"]:
            raise RuntimeError("脚本执行完成但未找到任务链接，请查看日志")

        on_progress(100, f"完成！任务链接已就绪")
        return result

    def run_with_scenario_set(
        self,
        links: List[str],
        product: str,
        on_progress: Callable,
        scenario_name: Optional[str] = None,
        mf_package: Optional[str] = None,
        project: Optional[str] = None,
        repos: str = "CP_FST",
        roi_start: Optional[str] = None,
        roi_end: Optional[str] = None,
        conservative: bool = False,
        em_mode: bool = False,
        use_ci_build: bool = False,
    ) -> dict:
        """先创建 Scenario Set，再创建仿真任务"""
        from .scenario_service import ScenarioService

        on_progress(5, "📦 第 1 步：创建 Scenario Set...")

        # 步骤 1: 创建 Scenario Set
        if not scenario_name:
            scenario_name = f"{product}_{datetime.now().strftime('%Y%m%d%H%M%S')}"

        scenario_service = ScenarioService()

        def scenario_progress(pct, msg):
            """将 Scenario Set 创建进度映射到 0-40%"""
            on_progress(int(pct * 0.4), f"📦 创建 Scenario Set: {msg}")

        scenario_result = scenario_service.run(
            name=scenario_name,
            links=links,
            repos=repos,
            on_progress=scenario_progress,
        )

        on_progress(45, f"✅ Scenario Set 创建成功！ID: {scenario_result['scenario_set_id']}")

        # 步骤 2: 使用 Scenario Set ID 创建仿真任务
        on_progress(50, "🚀 第 2 步：创建仿真任务...")

        scenario_set_id = scenario_result["scenario_set_id"]

        def simict_progress(pct, msg):
            """将仿真任务创建进度映射到 50-100%"""
            on_progress(50 + int(pct * 0.5), f"🚀 创建仿真: {msg}")

        simict_result = self.run(
            input_link=scenario_set_id,
            product=product,
            on_progress=simict_progress,
            mf_package=mf_package,
            project=project,
            roi_start=roi_start,
            roi_end=roi_end,
            conservative=conservative,
            em_mode=em_mode,
            use_ci_build=use_ci_build,
        )

        on_progress(100, "✅ 完成！")

        # 返回包含两个链接的结果
        return {
            "scenario_set_id": scenario_result["scenario_set_id"],
            "scenario_set_name": scenario_result["scenario_set_name"],
            "scenario_set_ess_url": scenario_result["ess_link"],
            "scenario_set_mviz_url": scenario_result["mviz_link"],
            "event_count": scenario_result["event_count"],
            "task_url": simict_result["task_url"],
            "product": product,
            "mode": simict_result["mode"],
        }
