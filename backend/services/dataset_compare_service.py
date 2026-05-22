"""
数据集对比与运算服务
包装 dataset_compare_script.py，通过 subprocess 调用并实时上报进度
"""
import sys
import subprocess
import json
from pathlib import Path
from typing import Callable, List, Optional

SCRIPT_PATH = Path(__file__).parent / "dataset_compare_script.py"
PYTHON = sys.executable


class DatasetCompareService:
    """
    run() 同步执行（通过 asyncio.to_thread 调用）
    on_progress(progress: int, message: str) 全程回调
    返回 dict: {sets, pairwise, operations, metadata, created_set}
    """

    def run(
        self,
        input_sets: List[dict],
        operations: List[str],
        difference_source: str,
        create_scenario_set: bool,
        scenario_set_name: Optional[str],
        scenario_set_operation: str,
        repos: str,
        owners: List[str],
        fetch_metadata: bool,
        on_progress: Callable,
    ) -> dict:
        on_progress(3, "正在准备参数...")

        input_json = json.dumps({
            "input_sets": input_sets,
            "operations": operations,
            "difference_source": difference_source,
            "create_scenario_set": create_scenario_set,
            "scenario_set_name": scenario_set_name,
            "scenario_set_operation": scenario_set_operation,
            "repos": repos,
            "owners": owners,
            "fetch_metadata": fetch_metadata,
        }, ensure_ascii=False)

        proc = subprocess.Popen(
            [PYTHON, str(SCRIPT_PATH)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            bufsize=1,
            cwd=str(SCRIPT_PATH.parent),
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
        )

        proc.stdin.write(input_json)
        proc.stdin.close()

        result = {}
        all_lines = []

        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            if not line:
                continue
            all_lines.append(line)

            # Parse PROGRESS:pct:msg
            if line.startswith("PROGRESS:"):
                parts = line.split(":", 2)
                if len(parts) == 3:
                    try:
                        pct = int(parts[1])
                        msg = parts[2]
                        on_progress(pct, msg)
                    except ValueError:
                        pass
                continue

            # Parse RESULT:{json}
            if line.startswith("RESULT:"):
                try:
                    result = json.loads(line[7:])
                except json.JSONDecodeError:
                    pass
                continue

            # Venv bootstrap messages
            if "首次运行" in line or "创建虚拟环境" in line:
                on_progress(2, "首次运行，正在初始化虚拟环境（约 1-3 分钟）...")
            elif "安装依赖" in line:
                on_progress(3, "正在安装依赖...")
            elif "虚拟环境创建完成" in line:
                on_progress(5, "虚拟环境就绪，开始处理...")

        proc.wait()

        if proc.returncode != 0:
            tail = "\n".join(all_lines[-30:])
            raise RuntimeError(f"脚本异常退出 (code={proc.returncode}):\n{tail}")

        if not result:
            raise RuntimeError("脚本执行完成但未返回结果")

        if "error" in result:
            raise RuntimeError(result["error"])

        on_progress(100, "对比完成！")
        return result
