"""FastAPI 主应用"""
from fastapi import FastAPI, BackgroundTasks, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pathlib import Path
import asyncio
import uuid
from typing import Dict
from datetime import datetime, timezone, timedelta
import json

_CST = timezone(timedelta(hours=8))

def now_cst() -> datetime:
    """返回不含时区信息的北京时间（与现有代码兼容）"""
    return datetime.now(_CST).replace(tzinfo=None)

from .models import (RoadtestRequest, SimBagRequest, FillSimRequest, SimictTaskRequest,
                     ScenarioSetRequest, CreateCaseRequest, FeedbackRequest, BugReportRequest,
                     TaskStatus, ProgressUpdate, EventDetail, PipelineRoadtestCaseRequest,
                     PipelineRoadtestSimRequest, PipelineFillSimRequest,
                     HarzExtractRequest, HarzUpdateWikiRequest,
                     MainlineCheckRequest, MainlineAddReleasesRequest,
                     DatasetCompareRequest, CreateScenarioSetRequest, ClaQueryRequest)
from .services.alp_service import analyze_task as alp_analyze_task, parse_task_id
from .services.alp_submit_service import submit_checker_task as alp_submit_checker
from .services.fst_import_service import import_to_fst
from .services.pipeline_db import list_runs, get_run, create_run, update_run, delete_run
from .services.dpi_poller import get_dpi_token, poll_once
from .services.ess_client import ESSClient
from .services.simbag_service import SimBagService
from .services.fill_sim_service import FillSimService
from .services.simict_service import SimictService
from .services.mainline_version_service import check_updates as mainline_check_updates, insert_releases_to_html as mainline_insert_html
from .services.scenario_service import ScenarioService
from .services.create_case_service import CreateCaseService
from .services.dataset_compare_service import DatasetCompareService
from .services.keycloak_auth import get_token as get_keycloak_token
import requests as _requests
from .services.feishu_client import FeishuClient, sync_token_from_cloud
from .services.feishu_bot import get_feishu_bot
from .services.harz_version_service import extract_from_doc, update_wiki as harz_update_wiki, get_package_list
from .config import get_settings
from .utils.logger import get_logger

settings = get_settings()
logger = get_logger(__name__)

app = FastAPI(
    title="ESS 路测数据自动整理",
    description="团队共享的路测数据自动整理工具",
    version="1.0.0"
)


@app.on_event("startup")
async def startup_feishu_token_keeper():
    """定时刷新飞书 token，防止 refresh_token 过期"""
    import asyncio
    from feishu_sync.retoken import get_access_token

    async def _keep_alive():
        while True:
            await asyncio.sleep(6 * 3600)  # 每6小时刷新一次
            try:
                token, ttl = await asyncio.to_thread(get_access_token)
                logger.info(f"Feishu token keep-alive refreshed, TTL={ttl}s")
            except Exception as e:
                logger.warning(f"Feishu token keep-alive failed: {e}")

    asyncio.create_task(_keep_alive())

# CORS 配置
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 静态文件服务
static_path = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

# 内存任务存储（生产环境建议用 Redis）
tasks: Dict[str, TaskStatus] = {}
task_queues: Dict[str, asyncio.Queue] = {}
pipeline_states: Dict[str, dict] = {}  # task_id → {type, request, completed_steps, failed_at}

# 统计数据存储
STATS_FILE = Path(__file__).parent / "static" / "stats.json"
TIME_SAVED_PER_TASK = {  # 每个功能节约的人工时间（分钟）
    "roadtest": 10, "simbag": 30, "fillsim": 45, "simict": 5,
    "scenarioset": 8, "createcase": 20, "pipe-case": 30, "pipe-sim": 20, "pipe-fillsim": 75,
    "harz-weekly": 8, "dataset-compare": 15,
}

def _load_stats() -> dict:
    if STATS_FILE.exists():
        try:
            return json.loads(STATS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"views": 0, "tasks": {}}
    return {"views": 0, "tasks": {}}

def _save_stats(stats: dict):
    STATS_FILE.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")

def _record_task(task_type: str):
    stats = _load_stats()
    stats["tasks"][task_type] = stats["tasks"].get(task_type, 0) + 1
    _save_stats(stats)


@app.get("/", response_class=HTMLResponse)
async def read_root():
    """主页面"""
    stats = _load_stats()
    stats["views"] = stats.get("views", 0) + 1
    _save_stats(stats)
    html_path = Path(__file__).parent / "static" / "index.html"
    return HTMLResponse(
        content=html_path.read_text(encoding="utf-8"),
        headers={"Cache-Control": "no-store"},
    )


@app.get("/api/stats")
async def get_stats():
    """获取统计数据"""
    stats = _load_stats()
    tasks_total = sum(stats.get("tasks", {}).values())
    time_saved = sum(count * TIME_SAVED_PER_TASK.get(k, 0) for k, count in stats.get("tasks", {}).items())
    return {
        "views": stats.get("views", 0),
        "tasks_total": tasks_total,
        "time_saved_minutes": time_saved,
        "tasks_by_type": stats.get("tasks", {}),
    }


@app.get("/api/releases")
async def get_releases():
    """返回 releases.json 的发版数据（持久化，不随 HTML 部署丢失）"""
    p = _releases_json_path()
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"releases": [], "mainlineR6": [], "mainlineR7": []}


@app.get("/api/internal/feishu-token")
async def get_feishu_token(key: str = ""):
    """内部 API：返回当前飞书 token 供本地同步使用"""
    if key != settings.FEISHU_TOKEN_SYNC_KEY:
        raise HTTPException(status_code=403, detail="Forbidden")
    import json
    from pathlib import Path
    result = {}
    for name in ("access_token", "refresh_token"):
        p = Path(f"~/.feishu/{name}.json").expanduser()
        if p.exists():
            result[name] = json.loads(p.read_text(encoding="utf-8"))
    if not result:
        raise HTTPException(status_code=404, detail="No token files found")
    return result


@app.get("/api/repos")
async def get_repos():
    """获取 ESS 仓库列表"""
    # 常用仓库列表（来自 https://ess.momenta.works/evaluation ）
    return {
        "repos": [
            "CP_FST", "NP_FST", "ADAS", "CP", "NP",
            "LSS", "ESS", "AES", "ABSM", "BSM",
            "BSD", "SLIF", "IHC", "CMSR",
            "L3_CFST", "MNP_FST", "ADAS_FST",
        ]
    }


@app.post("/api/submit")
async def submit_task(request: RoadtestRequest, background_tasks: BackgroundTasks):
    """提交路测数据整理任务"""
    _record_task("roadtest")
    task_id = str(uuid.uuid4())

    # 初始化任务状态
    tasks[task_id] = TaskStatus(
        task_id=task_id,
        status="pending",
        progress=0,
        message="任务已提交，等待处理...",
        created_at=now_cst(),
        updated_at=now_cst()
    )

    # 创建进度队列
    task_queues[task_id] = asyncio.Queue()

    # 后台执行任务
    background_tasks.add_task(process_task, task_id, request)

    return {"task_id": task_id, "message": "任务已提交"}


@app.get("/api/task/{task_id}")
async def get_task_status(task_id: str):
    """查询任务状态"""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="任务不存在")
    return tasks[task_id]


@app.get("/api/task/{task_id}/stream")
async def stream_progress(task_id: str):
    """SSE 实时进度推送"""
    if task_id not in task_queues:
        raise HTTPException(status_code=404, detail="任务不存在")

    async def event_generator():
        queue = task_queues[task_id]
        try:
            while True:
                # 等待新进度消息
                update = await queue.get()

                # SSE 格式
                data = update.model_dump_json()
                yield f"data: {data}\n\n"

                # 任务完成或失败时断开连接
                if update.type in ["completed", "error"]:
                    break
        except asyncio.CancelledError:
            logger.info(f"SSE stream cancelled for task {task_id}")
        finally:
            # 清理队列
            if task_id in task_queues:
                del task_queues[task_id]

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        }
    )


async def process_task(task_id: str, request: RoadtestRequest):
    """后台任务处理逻辑"""
    queue = task_queues[task_id]

    try:
        # Step 1: 登录 ESS
        await update_task(task_id, "running", 10, "正在登录 ESS...")
        await queue.put(ProgressUpdate(
            type="progress",
            message="正在登录 ESS...",
            progress=10
        ))

        ess_client = ESSClient()
        await asyncio.to_thread(
            ess_client.login,
            settings.ESS_DEFAULT_USERNAME,
            settings.ESS_DEFAULT_PASSWORD
        )

        # Step 2: 查询事件
        await update_task(task_id, "running", 30, "正在查询事件列表...")
        await queue.put(ProgressUpdate(
            type="progress",
            message="正在查询事件列表...",
            progress=30
        ))

        def progress_callback(current, total, message):
            """同步回调，更新任务状态"""
            if total:
                progress = 30 + int((current / total) * 40)
            else:
                progress = 30

            # 更新内存状态
            tasks[task_id].progress = progress
            tasks[task_id].message = message
            tasks[task_id].updated_at = now_cst()

        events = await asyncio.to_thread(
            ess_client.fetch_events,
            request.plate,
            request.start_time + ":00",
            request.end_time + ":00",
            progress_callback
        )

        await queue.put(ProgressUpdate(
            type="event_count",
            message=f"共找到 {len(events)} 个事件",
            progress=70,
            data={"count": len(events)}
        ))

        # Step 3: 创建 shareKey
        await update_task(task_id, "running", 75, "正在创建 ESS 分享链接...")
        share_url = await asyncio.to_thread(
            ess_client.create_share_key,
            request.plate,
            request.start_time + ":00",
            request.end_time + ":00"
        )

        # Step 4: 创建飞书文档
        await update_task(task_id, "running", 85, "正在创建飞书文档...")
        await queue.put(ProgressUpdate(
            type="progress",
            message="正在创建飞书文档...",
            progress=85
        ))

        # 提取 package_version（取第一个非空值）
        package_version = ""
        for event in events:
            if event.get("package_version"):
                package_version = event["package_version"]
                break

        feishu_client = FeishuClient()
        feishu_url = await asyncio.to_thread(
            feishu_client.create_document,
            request.plate,
            request.start_time + ":00",
            request.end_time + ":00",
            share_url,
            package_version,
            events
        )

        # Step 5: 完成
        tasks[task_id].status = "completed"
        tasks[task_id].progress = 100
        tasks[task_id].message = "任务完成！"
        tasks[task_id].events = [EventDetail(**e) for e in events]
        tasks[task_id].feishu_url = feishu_url
        tasks[task_id].updated_at = now_cst()

        await queue.put(ProgressUpdate(
            type="completed",
            message="任务完成！",
            progress=100,
            data={
                "feishu_url": feishu_url,
                "ess_share_url": share_url,
                "event_count": len(events),
                "plate": request.plate,
                "start_time": request.start_time,
                "end_time": request.end_time,
                "events": [
                    {
                        "name": e["name"],
                        "description": e["description"][:100] + "..." if len(e["description"]) > 100 else e["description"],
                        "ess_url": e["ess_url"],
                        "mviz_url": e["mviz_url"]
                    }
                    for e in events
                ]
            }
        ))

    except Exception as e:
        logger.exception(f"Task {task_id} failed")
        tasks[task_id].status = "failed"
        tasks[task_id].error = str(e)
        tasks[task_id].updated_at = now_cst()

        await queue.put(ProgressUpdate(
            type="error",
            message=f"任务失败：{str(e)}",
            progress=tasks[task_id].progress
        ))


async def update_task(task_id: str, status: str, progress: int, message: str):
    """更新任务状态"""
    tasks[task_id].status = status
    tasks[task_id].progress = progress
    tasks[task_id].message = message
    tasks[task_id].updated_at = now_cst()


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "healthy", "timestamp": now_cst().isoformat()}


# ── SimICT 仿真任务创建 ───────────────────────────────────────────
@app.post("/api/simict/submit")
async def submit_simict_task(request: SimictTaskRequest, background_tasks: BackgroundTasks):
    """提交 SimICT 仿真任务创建"""
    _record_task("simict")
    task_id = str(uuid.uuid4())

    tasks[task_id] = TaskStatus(
        task_id=task_id,
        status="pending",
        progress=0,
        message="任务已提交，等待处理...",
        created_at=now_cst(),
        updated_at=now_cst()
    )

    task_queues[task_id] = asyncio.Queue()
    background_tasks.add_task(process_simict_task, task_id, request)

    return {"task_id": task_id, "message": "任务已提交"}


async def process_simict_task(task_id: str, request: SimictTaskRequest):
    """后台 SimICT 任务处理 - 支持多个链接先创建 Scenario Set，再创建仿真"""
    queue = task_queues[task_id]
    loop = asyncio.get_event_loop()

    def on_progress(progress: int, message: str):
        tasks[task_id].progress = progress
        tasks[task_id].message = message
        tasks[task_id].updated_at = now_cst()
        loop.call_soon_threadsafe(
            queue.put_nowait,
            ProgressUpdate(type="progress", message=message, progress=progress)
        )

    try:
        service = SimictService()

        # 检查是否有多个链接
        if len(request.links) > 1:
            # 多个链接：先创建 Scenario Set，再创建仿真任务
            result = await asyncio.to_thread(
                service.run_with_scenario_set,
                request.links,
                request.product,
                on_progress,
                request.scenario_name,
                request.mf_package,
                request.project,
                request.repos,
                request.roi_start,
                request.roi_end,
                request.conservative,
                request.em_mode,
                request.use_ci_build,
            )
        else:
            # 单个链接：直接创建仿真任务
            result = await asyncio.to_thread(
                service.run,
                request.links[0],
                request.product,
                on_progress,
                request.mf_package,
                request.project,
                request.roi_start,
                request.roi_end,
                request.conservative,
                request.em_mode,
                request.use_ci_build,
            )

        tasks[task_id].status = "completed"
        tasks[task_id].progress = 100
        tasks[task_id].message = "任务创建成功！"
        tasks[task_id].updated_at = now_cst()

        # 返回数据：包含 Scenario Set 信息（如果有）
        response_data = {
            "task_url": result["task_url"],
            "product": result["product"],
            "mode": result["mode"],
        }

        # 如果创建了 Scenario Set，添加到返回数据中
        if "scenario_set_id" in result:
            response_data.update({
                "scenario_set_id": result["scenario_set_id"],
                "scenario_set_name": result["scenario_set_name"],
                "scenario_set_ess_url": result.get("scenario_set_ess_url", ""),
                "scenario_set_mviz_url": result.get("scenario_set_mviz_url", ""),
                "event_count": result.get("event_count", 0),
            })

        await queue.put(ProgressUpdate(
            type="completed",
            message="任务创建成功！",
            progress=100,
            data=response_data
        ))

    except Exception as e:
        logger.exception(f"SimICT task {task_id} failed")
        tasks[task_id].status = "failed"
        tasks[task_id].error = str(e)
        tasks[task_id].updated_at = now_cst()

        await queue.put(ProgressUpdate(
            type="error",
            message=f"任务失败：{str(e)}",
            progress=tasks[task_id].progress
        ))


@app.post("/api/scenarioset/submit")
async def submit_scenarioset_task(request: ScenarioSetRequest, background_tasks: BackgroundTasks):
    """提交创建 Scenario Set 任务"""
    _record_task("scenarioset")
    task_id = str(uuid.uuid4())

    tasks[task_id] = TaskStatus(
        task_id=task_id,
        status="pending",
        progress=0,
        message="任务已提交，等待处理...",
        created_at=now_cst(),
        updated_at=now_cst()
    )

    task_queues[task_id] = asyncio.Queue()
    background_tasks.add_task(process_scenarioset_task, task_id, request)

    return {"task_id": task_id, "message": "任务已提交"}


async def process_scenarioset_task(task_id: str, request: ScenarioSetRequest):
    """后台 Scenario Set 创建任务处理"""
    queue = task_queues[task_id]
    loop = asyncio.get_event_loop()

    def on_progress(progress: int, message: str):
        tasks[task_id].progress = progress
        tasks[task_id].message = message
        tasks[task_id].updated_at = now_cst()
        loop.call_soon_threadsafe(
            queue.put_nowait,
            ProgressUpdate(type="progress", message=message, progress=progress)
        )

    try:
        service = ScenarioService()
        result = await asyncio.to_thread(
            service.run,
            request.name,
            request.links,
            request.repos,
            on_progress,
        )

        tasks[task_id].status = "completed"
        tasks[task_id].progress = 100
        tasks[task_id].message = "Scenario Set 创建成功！"
        tasks[task_id].updated_at = now_cst()

        await queue.put(ProgressUpdate(
            type="completed",
            message="Scenario Set 创建成功！",
            progress=100,
            data={
                "scenario_set_id": result["scenario_set_id"],
                "scenario_set_name": result["scenario_set_name"],
                "event_count": result["event_count"],
                "ess_link": result["ess_link"],
                "mviz_link": result["mviz_link"],
                "failed_links": result["failed_links"],
            }
        ))

    except Exception as e:
        logger.exception(f"ScenarioSet task {task_id} failed")
        tasks[task_id].status = "failed"
        tasks[task_id].error = str(e)
        tasks[task_id].updated_at = now_cst()

        await queue.put(ProgressUpdate(
            type="error",
            message=f"任务失败：{str(e)}",
            progress=tasks[task_id].progress
        ))


# ── 数据集对比与运算 ────────────────────────────────────────────────
@app.post("/api/dataset-compare/submit")
async def submit_dataset_compare(request: DatasetCompareRequest, background_tasks: BackgroundTasks):
    """提交数据集对比与运算任务"""
    _record_task("dataset-compare")
    task_id = str(uuid.uuid4())

    tasks[task_id] = TaskStatus(
        task_id=task_id, status="pending", progress=0,
        message="任务已提交，等待处理...",
        created_at=now_cst(), updated_at=now_cst()
    )
    task_queues[task_id] = asyncio.Queue()
    background_tasks.add_task(process_dataset_compare_task, task_id, request)
    return {"task_id": task_id, "message": "任务已提交"}


async def process_dataset_compare_task(task_id: str, request: DatasetCompareRequest):
    """后台数据集对比任务处理"""
    queue = task_queues[task_id]
    loop = asyncio.get_event_loop()

    def on_progress(progress: int, message: str):
        tasks[task_id].progress = progress
        tasks[task_id].message = message
        tasks[task_id].updated_at = now_cst()
        loop.call_soon_threadsafe(
            queue.put_nowait,
            ProgressUpdate(type="progress", message=message, progress=progress)
        )

    try:
        service = DatasetCompareService()
        result = await asyncio.to_thread(
            service.run,
            request.input_sets,
            request.operations,
            request.difference_source,
            request.create_scenario_set,
            request.scenario_set_name,
            request.scenario_set_operation,
            request.repos,
            request.scenario_set_owners,
            request.fetch_metadata,
            on_progress,
        )

        tasks[task_id].status = "completed"
        tasks[task_id].progress = 100
        tasks[task_id].message = "对比完成！"
        tasks[task_id].updated_at = now_cst()

        await queue.put(ProgressUpdate(
            type="completed",
            message="对比完成！",
            progress=100,
            data=result
        ))

    except Exception as e:
        logger.exception(f"DatasetCompare task {task_id} failed")
        tasks[task_id].status = "failed"
        tasks[task_id].error = str(e)


@app.post("/api/dataset-compare/create-set")
async def create_scenario_set_direct(request: CreateScenarioSetRequest):
    """直接从 event_ids 创建 Scenario Set，结果在当前页展示"""
    import urllib.parse
    ESS_BASE = "https://ess.momenta.works"
    _requests.packages.urllib3.disable_warnings()

    try:
        token = get_keycloak_token()
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

        chunks = [request.event_ids[i:i+1000] for i in range(0, len(request.event_ids), 1000)]
        for chunk in chunks:
            body = {"repos": request.repos, "name": request.name, "event_ids": chunk}
            if request.owners:
                body["owners"] = request.owners
            resp = _requests.post(
                f"{ESS_BASE}/api/v1/scenario-set/_import/event",
                json=body, headers=headers, timeout=120, verify=False,
            )
            resp.raise_for_status()

        # 获取刚创建的 set ID
        r = _requests.get(
            f"{ESS_BASE}/api/v1/scenario-set/{request.repos}/{urllib.parse.quote(request.name, safe='')}",
            headers=headers, timeout=30, verify=False,
        )
        set_id = r.json().get("id", "") if r.status_code == 200 else ""
        name_enc = urllib.parse.quote(request.name, safe='')

        return {
            "id": set_id,
            "name": request.name,
            "event_count": len(request.event_ids),
            "ess_link": f"https://ess.momenta.works/evaluation/scenario_set/detail?repos={request.repos}&name={name_enc}",
            "mviz_link": f"https://mviz.momenta.works/player/v5/scenario-set/{set_id}" if set_id else "",
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        tasks[task_id].updated_at = now_cst()

        await queue.put(ProgressUpdate(
            type="error",
            message=f"任务失败：{str(e)}",
            progress=tasks[task_id].progress
        ))
async def submit_createcase_task(request: CreateCaseRequest, background_tasks: BackgroundTasks):
    """提交批量创建飞书 Case 任务"""
    _record_task("createcase")
    task_id = str(uuid.uuid4())
    tasks[task_id] = TaskStatus(
        task_id=task_id, status="pending", progress=0,
        message="任务已提交，等待处理...",
        created_at=now_cst(), updated_at=now_cst()
    )
    task_queues[task_id] = asyncio.Queue()
    background_tasks.add_task(process_createcase_task, task_id, request)
    return {"task_id": task_id, "message": "任务已提交"}


async def process_createcase_task(task_id: str, request: CreateCaseRequest):
    queue = task_queues[task_id]
    loop = asyncio.get_event_loop()

    def on_progress(progress: int, message: str):
        tasks[task_id].progress = progress
        tasks[task_id].message = message
        tasks[task_id].updated_at = now_cst()
        loop.call_soon_threadsafe(
            queue.put_nowait,
            ProgressUpdate(type="progress", message=message, progress=progress)
        )

    try:
        service = CreateCaseService()
        result = await asyncio.to_thread(
            service.run,
            request.links,
            request.product,
            request.feishu_operator,
            request.feishu_email,
            on_progress,
        )
        tasks[task_id].status = "completed"
        tasks[task_id].progress = 100
        tasks[task_id].updated_at = now_cst()
        await queue.put(ProgressUpdate(
            type="completed", message=result["summary"], progress=100,
            data={"ok": result["ok"], "fail": result["fail"], "links": result["links"]}
        ))
    except Exception as e:
        logger.exception(f"CreateCase task {task_id} failed")
        tasks[task_id].status = "failed"
        tasks[task_id].error = str(e)
        tasks[task_id].updated_at = now_cst()
        await queue.put(ProgressUpdate(
            type="error", message=f"任务失败：{str(e)}",
            progress=tasks[task_id].progress
        ))


# ── 功能需求反馈 ──────────────────────────────────────────────────
FEEDBACK_FILE = Path(__file__).parent / "static" / "feedback.json"


def _load_feedback() -> list:
    if FEEDBACK_FILE.exists():
        try:
            return json.loads(FEEDBACK_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_feedback(items: list):
    FEEDBACK_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/feedback")
async def get_feedback():
    return _load_feedback()


@app.post("/api/feedback")
async def post_feedback(req: FeedbackRequest):
    items = _load_feedback()
    item = {
        "id": str(uuid.uuid4())[:8],
        "name": req.name or "匿名",
        "content": req.content,
        "time": now_cst().strftime("%Y-%m-%d %H:%M"),
        "votes": 0,
    }
    items.insert(0, item)
    _save_feedback(items)
    return item


@app.post("/api/feedback/{item_id}/vote")
async def vote_feedback(item_id: str):
    items = _load_feedback()
    for item in items:
        if item["id"] == item_id:
            item["votes"] = item.get("votes", 0) + 1
            _save_feedback(items)
            return {"votes": item["votes"]}
    raise HTTPException(status_code=404, detail="Not found")


@app.delete("/api/feedback/{item_id}")
async def delete_feedback(item_id: str):
    items = _load_feedback()
    new_items = [i for i in items if i["id"] != item_id]
    if len(new_items) == len(items):
        raise HTTPException(status_code=404, detail="Not found")
    _save_feedback(new_items)
    return {"ok": True}


# ── 问题反馈 ───────────────────────────────────────────────────────
BUGREPORT_FILE = Path(__file__).parent / "static" / "bugreport.json"


def _load_bugreport() -> list:
    if BUGREPORT_FILE.exists():
        try:
            return json.loads(BUGREPORT_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_bugreport(items: list):
    BUGREPORT_FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/bugreport")
async def get_bugreport():
    return _load_bugreport()


@app.post("/api/bugreport")
async def post_bugreport(req: BugReportRequest):
    items = _load_bugreport()
    item = {
        "id": str(uuid.uuid4())[:8],
        "name": req.name or "匿名",
        "content": req.content,
        "time": now_cst().strftime("%Y-%m-%d %H:%M"),
        "status": "待处理",
    }
    items.insert(0, item)
    _save_bugreport(items)
    return item


@app.post("/api/bugreport/{item_id}/vote")
async def vote_bugreport(item_id: str):
    items = _load_bugreport()
    for item in items:
        if item["id"] == item_id:
            item["votes"] = item.get("votes", 0) + 1
            _save_bugreport(items)
            return {"votes": item["votes"]}
    raise HTTPException(status_code=404, detail="Not found")


@app.delete("/api/bugreport/{item_id}")
async def delete_bugreport(item_id: str):
    items = _load_bugreport()
    new_items = [i for i in items if i["id"] != item_id]
    if len(new_items) == len(items):
        raise HTTPException(status_code=404, detail="Not found")
    _save_bugreport(new_items)
    return {"ok": True}


@app.post("/api/simbag/submit")
async def submit_simbag_task(request: SimBagRequest, background_tasks: BackgroundTasks):
    """提交 sim-bag-match 任务"""
    _record_task("simbag")
    task_id = str(uuid.uuid4())

    tasks[task_id] = TaskStatus(
        task_id=task_id,
        status="pending",
        progress=0,
        message="任务已提交，等待处理...",
        created_at=now_cst(),
        updated_at=now_cst()
    )

    task_queues[task_id] = asyncio.Queue()
    background_tasks.add_task(process_simbag_task, task_id, request.batch_url)

    return {"task_id": task_id, "message": "任务已提交"}


async def process_simbag_task(task_id: str, batch_url: str):
    """后台 sim-bag-match 任务处理"""
    queue = task_queues[task_id]
    loop = asyncio.get_event_loop()

    def on_progress(progress: int, message: str):
        tasks[task_id].progress = progress
        tasks[task_id].message = message
        tasks[task_id].updated_at = now_cst()
        loop.call_soon_threadsafe(
            queue.put_nowait,
            ProgressUpdate(type="progress", message=message, progress=progress)
        )

    try:
        service = SimBagService()
        result = await asyncio.to_thread(service.run, batch_url, on_progress)

        tasks[task_id].status = "completed"
        tasks[task_id].progress = 100
        tasks[task_id].message = "任务完成！"
        tasks[task_id].updated_at = now_cst()

        await queue.put(ProgressUpdate(
            type="completed",
            message="任务完成！",
            progress=100,
            data={
                "feishu_url": result["feishu_url"],
                "doc_title": result["doc_title"],
                "event_count": result["event_count"]
            }
        ))

    except Exception as e:
        logger.exception(f"SimBag task {task_id} failed")
        tasks[task_id].status = "failed"
        tasks[task_id].error = str(e)
        tasks[task_id].updated_at = now_cst()

        await queue.put(ProgressUpdate(
            type="error",
            message=f"任务失败：{str(e)}",
            progress=tasks[task_id].progress
        ))


@app.post("/api/fillsim/submit")
async def submit_fillsim_task(request: FillSimRequest, background_tasks: BackgroundTasks):
    """提交填写仿真结果任务"""
    _record_task("fillsim")
    task_id = str(uuid.uuid4())

    tasks[task_id] = TaskStatus(
        task_id=task_id,
        status="pending",
        progress=0,
        message="任务已提交，等待处理...",
        created_at=now_cst(),
        updated_at=now_cst()
    )

    task_queues[task_id] = asyncio.Queue()
    background_tasks.add_task(process_fillsim_task, task_id, request)

    return {"task_id": task_id, "message": "任务已提交"}


async def process_fillsim_task(task_id: str, request: FillSimRequest):
    """后台填写仿真结果任务处理"""
    queue = task_queues[task_id]
    loop = asyncio.get_event_loop()

    def on_progress(progress: int, message: str):
        tasks[task_id].progress = progress
        tasks[task_id].message = message
        tasks[task_id].updated_at = now_cst()
        loop.call_soon_threadsafe(
            queue.put_nowait,
            ProgressUpdate(type="progress", message=message, progress=progress)
        )

    try:
        service = FillSimService()
        result = await asyncio.to_thread(
            service.run,
            request.batch_url,
            request.view_url,
            request.version,
            on_progress
        )

        tasks[task_id].status = "completed"
        tasks[task_id].progress = 100
        tasks[task_id].message = "任务完成！"
        tasks[task_id].updated_at = now_cst()

        await queue.put(ProgressUpdate(
            type="completed",
            message="任务完成！",
            progress=100,
            data={
                "ok": result["ok"],
                "fail": result["fail"],
                "skip": result["skip"],
            }
        ))

    except Exception as e:
        logger.exception(f"FillSim task {task_id} failed")
        tasks[task_id].status = "failed"
        tasks[task_id].error = str(e)
        tasks[task_id].updated_at = now_cst()

        await queue.put(ProgressUpdate(
            type="error",
            message=f"任务失败：{str(e)}",
            progress=tasks[task_id].progress
        ))


# ── 每周发版 ─────────────────────────────────────────────────────

@app.post("/api/harz/extract")
async def harz_extract(request: HarzExtractRequest):
    """从 Harz weekly release 文档提取版本信息（同步，约 3-5 秒）"""
    _record_task("harz-weekly")
    try:
        data = await asyncio.to_thread(extract_from_doc, request.release_url)
        return {"ok": True, "data": data}
    except Exception as e:
        logger.exception("harz extract failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/harz/add-release")
async def harz_add_release(request: HarzUpdateWikiRequest):
    """将新发版条目直接写入网页 releases 数组"""
    try:
        data = {
            "date": request.date, "st35_master": request.st35_master,
            "st35_weekly": request.st35_weekly, "st3_master": request.st3_master,
            "st3_weekly": request.st3_weekly, "devcar_7v": request.devcar_7v,
        }
        _insert_harz_release_to_html(data, request.release_url, request.doc_title)
        return {"ok": True}
    except Exception as e:
        logger.exception("harz add-release failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/harz/update-wiki")
async def harz_update_wiki_endpoint(request: HarzUpdateWikiRequest, background_tasks: BackgroundTasks):
    """插入新行到版本表 wiki（后台任务 + SSE）"""
    task_id = str(uuid.uuid4())
    tasks[task_id] = TaskStatus(task_id=task_id, status="pending", progress=0,
                                message="任务已提交...", created_at=now_cst(), updated_at=now_cst())
    task_queues[task_id] = asyncio.Queue()
    background_tasks.add_task(_harz_update_wiki_task, task_id, request)
    return {"task_id": task_id}


def _releases_json_path() -> Path:
    return Path(__file__).parent / "static" / "releases.json"


def _load_releases_json() -> dict:
    p = _releases_json_path()
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {"releases": [], "mainlineR6": [], "mainlineR7": []}


def _save_releases_json(data: dict):
    _releases_json_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _insert_harz_release_to_html(data: dict, release_url: str, doc_title: str):
    """将新发版条目写入 releases.json（持久化）并更新 index.html（兼容）"""
    # 1. 写 releases.json（持久化，不随部署丢失）
    rdata = _load_releases_json()
    if any(r.get("docUrl") == release_url for r in rdata["releases"]):
        logger.info(f"Release {data['date']} already in releases.json, skipping")
    else:
        new_entry = {
            "date": data["date"], "docTitle": doc_title, "docUrl": release_url,
            "st35Master": data["st35_master"], "st35Weekly": data["st35_weekly"],
            "st3Master": data["st3_master"], "st3Weekly": data["st3_weekly"],
            "devcar7v": data["devcar_7v"],
        }
        rdata["releases"].insert(0, new_entry)
        _save_releases_json(rdata)
        logger.info(f"Inserted release {data['date']} into releases.json")

    # 2. 同时写 index.html（保持原有逻辑兼容本地）
    html_path = Path(__file__).parent / "static" / "index.html"
    content = html_path.read_text(encoding="utf-8")
    if f"docUrl:'{release_url}'" in content:
        return
    indent = "                        "
    new_entry_js = (
        f"{indent}{{ date:'{data['date']}', docTitle:'{doc_title}', docUrl:'{release_url}',\n"
        f"{indent}  st35Master:'{data['st35_master']}', st35Weekly:'{data['st35_weekly']}',\n"
        f"{indent}  st3Master:'{data['st3_master']}', st3Weekly:'{data['st3_weekly']}',\n"
        f"{indent}  devcar7v:'{data['devcar_7v']}' }},\n"
    )
    content = content.replace("releases: [\n", "releases: [\n" + new_entry_js, 1)
    html_path.write_text(content, encoding="utf-8")
    logger.info(f"Inserted release {data['date']} into index.html")


async def _harz_update_wiki_task(task_id: str, request: HarzUpdateWikiRequest):
    queue = task_queues[task_id]
    loop = asyncio.get_event_loop()

    def on_progress(pct: int, msg: str):
        tasks[task_id].progress = pct
        tasks[task_id].message = msg
        tasks[task_id].updated_at = now_cst()
        loop.call_soon_threadsafe(queue.put_nowait,
                                  ProgressUpdate(type="progress", message=msg, progress=pct))

    try:
        data = {
            "date": request.date, "st35_master": request.st35_master,
            "st35_weekly": request.st35_weekly, "st3_master": request.st3_master,
            "st3_weekly": request.st3_weekly, "devcar_7v": request.devcar_7v,
        }
        wiki_url = await asyncio.to_thread(
            harz_update_wiki, request.release_url, request.doc_title, data, on_progress
        )
        # 同步写入 index.html releases 数组
        _insert_harz_release_to_html(data, request.release_url, request.doc_title)
        tasks[task_id].status = "completed"
        tasks[task_id].progress = 100
        tasks[task_id].updated_at = now_cst()
        release_entry = {
            "date": request.date,
            "docTitle": request.doc_title,
            "docUrl": request.release_url,
            "st35Master": request.st35_master,
            "st35Weekly": request.st35_weekly,
            "st3Master": request.st3_master,
            "st3Weekly": request.st3_weekly,
            "devcar7v": request.devcar_7v,
        }
        await queue.put(ProgressUpdate(type="completed", message="版本表更新完成！", progress=100,
                                       data={"wiki_url": wiki_url, "release": release_entry}))
    except Exception as e:
        logger.exception(f"harz update wiki task {task_id} failed")
        tasks[task_id].status = "failed"
        tasks[task_id].error = str(e)
        tasks[task_id].updated_at = now_cst()
        await queue.put(ProgressUpdate(type="error", message=f"更新失败：{str(e)}",
                                       progress=tasks[task_id].progress))


@app.post("/api/mainline/check-updates")
async def mainline_check_updates_endpoint(request: MainlineCheckRequest, background_tasks: BackgroundTasks):
    """检查主线版本更新（后台任务 + SSE）"""
    task_id = str(uuid.uuid4())
    tasks[task_id] = TaskStatus(task_id=task_id, status="pending", progress=0,
                                message="任务已提交...", created_at=now_cst(), updated_at=now_cst())
    task_queues[task_id] = asyncio.Queue()
    background_tasks.add_task(_mainline_check_task, task_id, request)
    return {"task_id": task_id}


async def _mainline_check_task(task_id: str, request: MainlineCheckRequest):
    queue = task_queues[task_id]
    loop = asyncio.get_event_loop()

    def on_progress(pct: int, msg: str):
        tasks[task_id].progress = pct
        tasks[task_id].message = msg
        tasks[task_id].updated_at = now_cst()
        loop.call_soon_threadsafe(queue.put_nowait,
                                  ProgressUpdate(type="progress", message=msg, progress=pct))

    try:
        result = await asyncio.to_thread(
            mainline_check_updates,
            request.ref_url_r6, request.ref_url_r7,
            request.current_r6_dates, request.current_r7_dates,
            on_progress,
        )
        tasks[task_id].status = "completed"
        tasks[task_id].progress = 100
        tasks[task_id].updated_at = now_cst()
        await queue.put(ProgressUpdate(type="completed", message="检查完成！", progress=100,
                                       data=result))
    except Exception as e:
        logger.exception(f"mainline check task {task_id} failed")
        tasks[task_id].status = "failed"
        tasks[task_id].error = str(e)
        tasks[task_id].updated_at = now_cst()
        await queue.put(ProgressUpdate(type="error", message=f"检查失败：{str(e)}",
                                       progress=tasks[task_id].progress))


@app.post("/api/mainline/add-releases")
async def mainline_add_releases(request: MainlineAddReleasesRequest):
    """将新版本写入 index.html"""
    try:
        mainline_insert_html(request.r6, request.r7)
        return {"ok": True}
    except Exception as e:
        logger.exception("mainline add-releases failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/packages/list")
async def get_packages_list(branch: str = "harz"):
    """获取可用的 DevCar 包列表（从飞书版本表动态读取）"""
    try:
        package_list = get_package_list(branch=branch)

        # 使用字符串日期排序，确保倒序（最新的在前）
        for key in ["harz", "mainline"]:
            if key in package_list:
                # 按日期字符串倒序（因为日期格式 yyyymmdd，字符串比较等同于日期比较）
                package_list[key] = sorted(
                    package_list[key],
                    key=lambda x: x.get("date", ""),
                    reverse=True
                )

        return {
            "success": True,
            "branch": branch,
            "harz": package_list.get("harz", []),
            "mainline": package_list.get("mainline", []),
        }
    except Exception as e:
        logger.exception(f"Failed to get package list")
        return {
            "success": False,
            "error": str(e),
            "harz": [],
            "mainline": [],
        }


# ── 一键全流程 ────────────────────────────────────────────────────

def _pipeline_task(task_id: str):
    tasks[task_id] = TaskStatus(task_id=task_id, status="pending", progress=0,
                                message="任务已提交，等待处理...",
                                created_at=now_cst(), updated_at=now_cst())
    task_queues[task_id] = asyncio.Queue()


def _make_emit(task_id: str, loop):
    def emit(type_: str, msg: str, pct: int, data=None):
        tasks[task_id].progress = pct
        tasks[task_id].message = msg
        tasks[task_id].updated_at = now_cst()
        loop.call_soon_threadsafe(task_queues[task_id].put_nowait,
                                  ProgressUpdate(type=type_, message=msg, progress=pct, data=data))
    return emit


# ── Pipeline 1: 路测整理 → 批量创建 Case ──────────────────────────
@app.post("/api/pipeline/roadtest-case/submit")
async def submit_pipeline_roadtest_case(request: PipelineRoadtestCaseRequest,
                                        background_tasks: BackgroundTasks):
    _record_task("pipe-case")
    task_id = str(uuid.uuid4())
    _pipeline_task(task_id)
    pipeline_states[task_id] = {"type": "roadtest-case", "request": request.dict(), "completed_steps": {}}
    background_tasks.add_task(process_pipeline_roadtest_case, task_id, request)
    return {"task_id": task_id}


async def process_pipeline_roadtest_case(task_id: str, request: PipelineRoadtestCaseRequest,
                                         from_step: int = 1, completed_steps: dict = None):
    completed_steps = completed_steps or {}
    loop = asyncio.get_event_loop()
    emit = _make_emit(task_id, loop)
    summary = []

    try:
        # Step 1
        if from_step <= 1:
            emit("progress", "步骤 1/2: 正在登录 ESS...", 5)
            ess_client = ESSClient()
            await asyncio.to_thread(ess_client.login, settings.ESS_DEFAULT_USERNAME, settings.ESS_DEFAULT_PASSWORD)

            def prog1(cur, total, msg):
                emit("progress", f"步骤 1/2: {msg}", 10 + int((cur / (total or 1)) * 35))

            events = await asyncio.to_thread(
                ess_client.fetch_events,
                request.plate, request.start_time + ":00", request.end_time + ":00", prog1)
            completed_steps["1"] = {"events": events}
            pipeline_states[task_id]["completed_steps"] = completed_steps
        else:
            events = completed_steps["1"]["events"]

        detail1 = f"找到 {len(events)} 个事件"
        summary.append({"step": 1, "name": "路测数据整理", "status": "done", "detail": detail1})
        emit("progress", f"步骤 1/2 完成：{detail1}，正在创建 Case...", 50)

        # Step 2
        links = [e["ess_url"] for e in events if e.get("ess_url")]

        def prog2(pct, msg):
            emit("progress", f"步骤 2/2: {msg}", 50 + int(pct * 0.5))

        result = await asyncio.to_thread(
            CreateCaseService().run,
            links, request.product, request.feishu_operator, request.feishu_email, prog2)

        detail2 = result.get("summary", f"成功 {result.get('ok', 0)} 个")
        summary.append({"step": 2, "name": "批量创建 Case", "status": "done", "detail": detail2})
        tasks[task_id].status = "completed"
        tasks[task_id].progress = 100
        tasks[task_id].updated_at = now_cst()
        await task_queues[task_id].put(ProgressUpdate(
            type="completed", message="全流程完成！", progress=100, data={"steps": summary}))

    except Exception as e:
        logger.exception(f"Pipeline roadtest-case {task_id} failed")
        failed_step = 2 if completed_steps.get("1") else 1
        pipeline_states[task_id]["failed_at"] = failed_step
        tasks[task_id].status = "failed"
        tasks[task_id].error = str(e)
        tasks[task_id].updated_at = now_cst()
        await task_queues[task_id].put(ProgressUpdate(
            type="error", message=f"步骤 {failed_step}/2 失败：{str(e)}",
            progress=tasks[task_id].progress,
            data={"failed_step": failed_step, "steps": summary,
                  "retry_available": bool(completed_steps.get("1"))}))


# ── Pipeline 2: 路测整理 → Scenario Set → 创建仿真任务 ────────────
@app.post("/api/pipeline/roadtest-sim/submit")
async def submit_pipeline_roadtest_sim(request: PipelineRoadtestSimRequest,
                                       background_tasks: BackgroundTasks):
    _record_task("pipe-sim")
    task_id = str(uuid.uuid4())
    _pipeline_task(task_id)
    pipeline_states[task_id] = {"type": "roadtest-sim", "request": request.dict(), "completed_steps": {}}
    background_tasks.add_task(process_pipeline_roadtest_sim, task_id, request)
    return {"task_id": task_id}


async def process_pipeline_roadtest_sim(task_id: str, request: PipelineRoadtestSimRequest,
                                        from_step: int = 1, completed_steps: dict = None):
    completed_steps = completed_steps or {}
    loop = asyncio.get_event_loop()
    emit = _make_emit(task_id, loop)
    summary = []

    try:
        # Step 1
        if from_step <= 1:
            emit("progress", "步骤 1/3: 正在登录 ESS...", 3)
            ess_client = ESSClient()
            await asyncio.to_thread(ess_client.login, settings.ESS_DEFAULT_USERNAME, settings.ESS_DEFAULT_PASSWORD)

            def prog1(cur, total, msg):
                emit("progress", f"步骤 1/3: {msg}", 5 + int((cur / (total or 1)) * 25))

            events = await asyncio.to_thread(
                ess_client.fetch_events,
                request.plate, request.start_time + ":00", request.end_time + ":00", prog1)
            completed_steps["1"] = {"events": events}
            pipeline_states[task_id]["completed_steps"] = completed_steps
        else:
            events = completed_steps["1"]["events"]

        detail1 = f"找到 {len(events)} 个事件"
        summary.append({"step": 1, "name": "路测数据整理", "status": "done", "detail": detail1})
        emit("progress", f"步骤 1/3 完成：{detail1}，正在创建 Scenario Set...", 33)

        # Step 2
        if from_step <= 2:
            links = [e["ess_url"] for e in events if e.get("ess_url")]
            from datetime import datetime as dt
            auto_name = f"{request.plate}_{dt.now().strftime('%m%d')}_roadtest"

            def prog2(pct, msg):
                emit("progress", f"步骤 2/3: {msg}", 33 + int(pct * 0.33))

            sc_result = await asyncio.to_thread(
                ScenarioService().run, auto_name, links, request.repos, prog2)
            completed_steps["2"] = {"ess_link": sc_result["ess_link"],
                                    "scenario_set_id": sc_result["scenario_set_id"]}
            pipeline_states[task_id]["completed_steps"] = completed_steps
        else:
            sc_result = {"ess_link": completed_steps["2"]["ess_link"]}

        detail2 = f"Scenario Set 已创建（{sc_result.get('event_count', len(events))} 个事件）"
        summary.append({"step": 2, "name": "创建 Scenario Set", "status": "done", "detail": detail2})
        emit("progress", f"步骤 2/3 完成，正在创建仿真任务...", 66)

        # Step 3
        def prog3(pct, msg):
            emit("progress", f"步骤 3/3: {msg}", 66 + int(pct * 0.34))

        sim_req_kwargs = dict(input_link=sc_result["ess_link"], product=request.product,
                              on_progress=prog3)
        if request.mf_package:
            sim_req_kwargs["mf_package"] = request.mf_package

        sim_result = await asyncio.to_thread(SimictService().run, **sim_req_kwargs)
        detail3 = f"仿真任务已创建：{sim_result.get('task_url', '')}"
        summary.append({"step": 3, "name": "创建仿真任务", "status": "done", "detail": detail3})
        tasks[task_id].status = "completed"
        tasks[task_id].progress = 100
        tasks[task_id].updated_at = now_cst()
        await task_queues[task_id].put(ProgressUpdate(
            type="completed", message="全流程完成！", progress=100,
            data={"steps": summary, "task_url": sim_result.get("task_url", "")}))

    except Exception as e:
        logger.exception(f"Pipeline roadtest-sim {task_id} failed")
        failed_step = (3 if completed_steps.get("2") else (2 if completed_steps.get("1") else 1))
        pipeline_states[task_id]["failed_at"] = failed_step
        tasks[task_id].status = "failed"
        tasks[task_id].error = str(e)
        tasks[task_id].updated_at = now_cst()
        await task_queues[task_id].put(ProgressUpdate(
            type="error", message=f"步骤 {failed_step}/3 失败：{str(e)}",
            progress=tasks[task_id].progress,
            data={"failed_step": failed_step, "steps": summary,
                  "retry_available": bool(completed_steps.get("1"))}))


# ── Pipeline 3: 原包匹配仿真 → 填写仿真结果 ──────────────────────
@app.post("/api/pipeline/fill-sim/submit")
async def submit_pipeline_fill_sim(request: PipelineFillSimRequest,
                                   background_tasks: BackgroundTasks):
    _record_task("pipe-fillsim")
    task_id = str(uuid.uuid4())
    _pipeline_task(task_id)
    pipeline_states[task_id] = {"type": "fill-sim", "request": request.dict(), "completed_steps": {}}
    background_tasks.add_task(process_pipeline_fill_sim, task_id, request)
    return {"task_id": task_id}


async def process_pipeline_fill_sim(task_id: str, request: PipelineFillSimRequest,
                                    from_step: int = 1, completed_steps: dict = None):
    completed_steps = completed_steps or {}
    loop = asyncio.get_event_loop()
    emit = _make_emit(task_id, loop)
    summary = []

    try:
        # Step 1
        if from_step <= 1:
            def prog1(pct, msg):
                emit("progress", f"步骤 1/2: {msg}", int(pct * 0.5))

            sb_result = await asyncio.to_thread(SimBagService().run, request.batch_url, prog1)
            completed_steps["1"] = {"feishu_url": sb_result["feishu_url"],
                                    "event_count": sb_result["event_count"]}
            pipeline_states[task_id]["completed_steps"] = completed_steps
        else:
            sb_result = completed_steps["1"]

        detail1 = f"飞书对比表已生成，{sb_result.get('event_count', '?')} 条记录"
        summary.append({"step": 1, "name": "原包匹配仿真", "status": "done",
                        "detail": detail1, "url": sb_result.get("feishu_url", "")})
        emit("progress", f"步骤 1/2 完成，正在写入 Case 解决方案...", 50)

        # Step 2
        def prog2(pct, msg):
            emit("progress", f"步骤 2/2: {msg}", 50 + int(pct * 0.5))

        fs_result = await asyncio.to_thread(
            FillSimService().run, request.batch_url, request.view_url, request.version, prog2)

        detail2 = f"写入成功 {fs_result['ok']}，失败 {fs_result['fail']}，跳过 {fs_result['skip']}"
        summary.append({"step": 2, "name": "填写仿真结果", "status": "done", "detail": detail2})
        tasks[task_id].status = "completed"
        tasks[task_id].progress = 100
        tasks[task_id].updated_at = now_cst()
        await task_queues[task_id].put(ProgressUpdate(
            type="completed", message="全流程完成！", progress=100,
            data={"steps": summary, "feishu_url": sb_result.get("feishu_url", "")}))

    except Exception as e:
        logger.exception(f"Pipeline fill-sim {task_id} failed")
        failed_step = 2 if completed_steps.get("1") else 1
        pipeline_states[task_id]["failed_at"] = failed_step
        tasks[task_id].status = "failed"
        tasks[task_id].error = str(e)
        tasks[task_id].updated_at = now_cst()
        await task_queues[task_id].put(ProgressUpdate(
            type="error", message=f"步骤 {failed_step}/2 失败：{str(e)}",
            progress=tasks[task_id].progress,
            data={"failed_step": failed_step, "steps": summary,
                  "retry_available": bool(completed_steps.get("1"))}))


# ── 流水线重试 ─────────────────────────────────────────────────────
@app.post("/api/pipeline/{orig_task_id}/retry")
async def retry_pipeline(orig_task_id: str, from_step: int,
                         background_tasks: BackgroundTasks):
    if orig_task_id not in pipeline_states:
        raise HTTPException(status_code=404, detail="Pipeline state not found")
    state = pipeline_states[orig_task_id]
    new_task_id = str(uuid.uuid4())
    _pipeline_task(new_task_id)
    pipeline_states[new_task_id] = {**state, "completed_steps": dict(state["completed_steps"])}

    pipeline_type = state["type"]
    req_data = state["request"]
    completed = state["completed_steps"]

    if pipeline_type == "roadtest-case":
        req = PipelineRoadtestCaseRequest(**req_data)
        background_tasks.add_task(process_pipeline_roadtest_case, new_task_id, req, from_step, completed)
    elif pipeline_type == "roadtest-sim":
        req = PipelineRoadtestSimRequest(**req_data)
        background_tasks.add_task(process_pipeline_roadtest_sim, new_task_id, req, from_step, completed)
    elif pipeline_type == "fill-sim":
        req = PipelineFillSimRequest(**req_data)
        background_tasks.add_task(process_pipeline_fill_sim, new_task_id, req, from_step, completed)
    else:
        raise HTTPException(status_code=400, detail="Unknown pipeline type")

    return {"task_id": new_task_id}


@app.post("/webhook/feishu")
async def feishu_webhook(request: Request, background_tasks: BackgroundTasks):
    """飞书机器人消息接收 Webhook"""
    # 记录原始请求
    logger.info(f"Received webhook request from {request.client.host}")
    logger.info(f"Headers: {dict(request.headers)}")

    # 解析 JSON 请求体
    try:
        data = await request.json()
        logger.info(f"Request body: {json.dumps(data, ensure_ascii=False)}")
    except Exception as e:
        logger.error(f"Failed to parse JSON: {e}")
        return {"error": "invalid json"}

    bot = get_feishu_bot()

    # 1. URL 验证（首次配置时飞书会发送验证请求）
    if data.get("type") == "url_verification":
        challenge = data.get("challenge", "")
        token = data.get("token", "")
        logger.info(f"Feishu URL verification received, token: {token}")

        # 如果配置了 verification_token，进行验证
        if settings.FEISHU_VERIFICATION_TOKEN:
            if token != settings.FEISHU_VERIFICATION_TOKEN:
                logger.warning("Invalid verification token")
                return {"error": "invalid token"}

        return {"challenge": challenge}

    # 2. 处理消息事件
    event = data.get("event", {})
    event_type = data.get("header", {}).get("event_type")

    if event_type == "im.message.receive_v1":
        # 提取消息信息
        message = event.get("message", {})
        sender = event.get("sender", {})
        chat_id = message.get("chat_id", "")
        message_type = message.get("message_type", "")
        message_id = message.get("message_id", "")

        # 只处理文本消息
        if message_type != "text":
            logger.info(f"Ignoring non-text message: {message_type}")
            return {"message": "ok"}

        # 解析消息内容
        try:
            content = json.loads(message.get("content", "{}"))
            text = content.get("text", "").strip()
        except:
            text = ""

        logger.info(f"Received message from {sender.get('sender_id', {}).get('open_id', '')}: {text}")

        # 判断是否是触发词或需要帮助
        trigger_words = ["整理路测", "路测整理", "整理", "帮助", "help"]
        is_trigger = any(word in text for word in trigger_words)

        if is_trigger or not text:
            # 发送欢迎卡片
            card = bot.create_welcome_card()
            background_tasks.add_task(
                bot.send_card_message,
                chat_id,
                card,
                "chat_id"
            )
            return {"message": "ok"}

        # 解析用户输入
        parsed = bot.parse_user_input(text)
        if not parsed:
            # 格式错误，重新发送欢迎卡片
            error_text = "❌ 格式不正确，请按照以下格式输入：\n\n车牌号 时间范围\n\n例如：IP5PM-4032 今天"
            background_tasks.add_task(
                bot.send_text_message,
                chat_id,
                error_text,
                "chat_id"
            )
            background_tasks.add_task(
                bot.send_card_message,
                chat_id,
                bot.create_welcome_card(),
                "chat_id"
            )
            return {"message": "ok"}

        plate, start_time, end_time = parsed

        # 发送处理中卡片
        processing_card = bot.create_processing_card(plate, start_time, end_time)
        background_tasks.add_task(
            bot.send_card_message,
            chat_id,
            processing_card,
            "chat_id"
        )

        # 后台执行路测整理任务
        background_tasks.add_task(
            process_bot_task,
            chat_id,
            plate,
            start_time,
            end_time,
            sender.get("sender_id", {}).get("open_id", "")
        )

        return {"message": "ok"}

    return {"message": "ok"}


async def process_bot_task(
    chat_id: str,
    plate: str,
    start_time: str,
    end_time: str,
    user_open_id: str
):
    """
    机器人场景的路测整理任务

    与 Web 场景的区别：
    1. 使用用户的 ESS 凭证（从内存/缓存读取，或使用默认凭证）
    2. 完成后发送结果卡片到聊天
    """
    bot = get_feishu_bot()
    task_start_time = now_cst()

    try:
        # Step 1: 登录 ESS
        ess_client = ESSClient()
        await asyncio.to_thread(
            ess_client.login,
            settings.ESS_DEFAULT_USERNAME,
            settings.ESS_DEFAULT_PASSWORD
        )

        # Step 2: 查询事件
        events = await asyncio.to_thread(
            ess_client.fetch_events,
            plate,
            start_time,
            end_time,
            lambda c, t, m: None  # 机器人场景不需要实时进度回调
        )

        # Step 3: 创建 shareKey
        share_url = await asyncio.to_thread(
            ess_client.create_share_key,
            plate,
            start_time,
            end_time
        )

        # Step 4: 创建飞书文档
        package_version = ""
        for event in events:
            if event.get("package_version"):
                package_version = event["package_version"]
                break

        feishu_client = FeishuClient()
        feishu_url = await asyncio.to_thread(
            feishu_client.create_document,
            plate,
            start_time,
            end_time,
            share_url,
            package_version,
            events
        )

        # Step 5: 计算统计信息
        task_end_time = now_cst()
        duration = (task_end_time - task_start_time).total_seconds()
        duration_str = f"{int(duration)}秒"

        start_dt = datetime.strptime(start_time, "%Y-%m-%d %H:%M:%S")
        end_dt = datetime.strptime(end_time, "%Y-%m-%d %H:%M:%S")
        time_span_days = (end_dt - start_dt).days
        if time_span_days == 0:
            time_span_str = "1天"
        else:
            time_span_str = f"{time_span_days + 1}天"

        # Step 6: 发送结果卡片
        result_card = bot.create_result_card(
            feishu_url,
            share_url,
            len(events),
            time_span_str,
            duration_str
        )
        await bot.send_card_message(chat_id, result_card, "chat_id")

    except Exception as e:
        logger.exception("Bot task failed")
        error_card = bot.create_error_card(str(e))
        await bot.send_card_message(chat_id, error_card, "chat_id")


# ── AES CUTIN 数据一条龙 Pipeline ────────────────────────────────

@app.post("/api/pipeline/merge-sets")
async def pipeline_merge_sets(request: Request):
    """将多个 Scenario Set 合并为一个，返回新 set 的 id 和 count。"""
    body = await request.json()
    set_ids = body.get("set_ids", [])
    name = body.get("name", "")
    repos = body.get("repos", "AES_FST")
    if not set_ids:
        raise HTTPException(status_code=400, detail="set_ids 不能为空")
    if not name:
        raise HTTPException(status_code=400, detail="name 不能为空")
    try:
        import requests as _req
        import re, urllib.parse
        from .services.keycloak_auth import get_token as _kc
        token = _kc()
        hdrs = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        ESS = "https://ess.momenta.works"

        def resolve_id(raw: str) -> str:
            """从 URL 或纯 ID 中提取 scenario_set_id"""
            raw = raw.strip()
            # 纯 24 位 hex ID
            if re.match(r'^[0-9a-f]{24}$', raw):
                return raw
            # URL 里提取 id= 或 name=
            m_id = re.search(r'[?&]id=([^&]+)', raw)
            if m_id:
                return urllib.parse.unquote(m_id.group(1))
            m_name = re.search(r'[?&]name=([^&]+)', raw)
            m_repo = re.search(r'[?&]repos=([^&]+)', raw)
            if m_name:
                name_val = urllib.parse.unquote(m_name.group(1))
                repo_val = urllib.parse.unquote(m_repo.group(1)) if m_repo else repos
                # 用 name 查询 ESS 拿 ID
                r = _req.post(f"{ESS}/api/v1/scenario-set/_list", headers=hdrs,
                              json={"name": name_val, "repos": repo_val})
                items = r.json().get("items", [])
                if items:
                    return items[0]["id"]
            return raw  # fallback

        def scan_set(sid):
            ids, marker = [], None
            resolved_id = resolve_id(sid)
            while True:
                scan_body = {"id": resolved_id, "repos": repos, "page_size": 100}
                if marker: scan_body["marker"] = marker
                r = _req.post(f"{ESS}/api/v1/scenario-set/_scan/scenario", headers=hdrs, json=scan_body)
                batch = r.json().get("items", [])
                ids.extend(x["event_id"] for x in batch)
                marker = r.json().get("marker")
                if len(batch) < 100 or not marker: break
            return ids

        all_ids = []
        for sid in set_ids:
            all_ids.extend(scan_set(sid.strip()))
        all_ids = list(dict.fromkeys(all_ids))

        # 用 _import/event 创建新 Scenario Set
        create_r = _req.post(f"{ESS}/api/v1/scenario-set/_import/event", headers=hdrs,
                             json={"repos": repos, "name": name, "event_ids": all_ids})
        result_id = create_r.json().get("id", "")
        return {"ok": True, "merged_id": result_id, "count": len(all_ids), "name": name}
    except Exception as e:
        logger.exception("merge sets failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/pipeline/dpi-launch")
async def pipeline_dpi_launch(request: Request):
    """触发 DPI Flyte 任务。支持 scenario_set_id（直接传 ID）或 dataset_input（自动解析）。"""
    body = await request.json()
    name             = body.get("name", "").strip()
    dpi_username     = body.get("dpi_username", "junhao.niu").strip()
    dpi_password     = body.get("dpi_password", "").strip()
    template_exec_id = body.get("template_exec_id", "").strip() or None
    if not name:         raise HTTPException(400, "name 不能为空")
    if not dpi_password: raise HTTPException(400, "dpi_password 不能为空")

    # 解析 scenario_set_id：优先用直传，否则从 dataset_input 解析
    scenario_set_id = body.get("scenario_set_id", "").strip()
    if not scenario_set_id:
        dataset_input = body.get("dataset_input", "").strip()
        if not dataset_input:
            raise HTTPException(400, "请提供 scenario_set_id 或 dataset_input")
        try:
            from .services.dpi_poller import resolve_scenario_set_id
            resolved = await asyncio.to_thread(resolve_scenario_set_id, dataset_input)
            scenario_set_id = resolved["id"]
        except ValueError as e:
            raise HTTPException(400, str(e))

    try:
        from .services.dpi_poller import get_dpi_token, create_dpi_execution
        dpi_token = await asyncio.to_thread(get_dpi_token, dpi_username, dpi_password)
        exec_id   = await asyncio.to_thread(
            create_dpi_execution, dpi_token, scenario_set_id, name, name, template_exec_id
        )
        return {"ok": True, "exec_id": exec_id, "scenario_set_id": scenario_set_id,
                "exec_url": f"https://dpi.dev.momenta.works/flyte-console/projects/ebm-infra/domains/dev/executions/{exec_id}"}
    except Exception as e:
        logger.exception("dpi-launch failed")
        raise HTTPException(500, str(e))


# ── DPI 模板管理 ──────────────────────────────────────────────────

_DPI_TPL_FILE = Path(__file__).parent / "static" / "dpi_templates.json"

def _load_dpi_templates() -> list:
    if _DPI_TPL_FILE.exists():
        try:
            return json.loads(_DPI_TPL_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    # 内置默认模板
    default = [{"id": "default", "name": "AES CUTIN 标准挖掘",
                "exec_id": "afjr4kzx4g9jtgh9gv2b", "note": "默认模板",
                "created_at": "2026-05-19"}]
    _DPI_TPL_FILE.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
    return default

def _save_dpi_templates(tpls: list):
    _DPI_TPL_FILE.write_text(json.dumps(tpls, ensure_ascii=False, indent=2), encoding="utf-8")


@app.get("/api/dpi/templates")
async def dpi_templates_list():
    return _load_dpi_templates()


@app.post("/api/dpi/templates")
async def dpi_templates_add(request: Request):
    """保存一个 DPI execution 作为模板（校验 exec_id 是否真实存在）。"""
    body = await request.json()
    exec_id  = body.get("exec_id", "").strip()
    name     = body.get("name", "").strip()
    note     = body.get("note", "").strip()
    dpi_username = body.get("dpi_username", "junhao.niu").strip()
    dpi_password = body.get("dpi_password", "").strip()
    if not exec_id:      raise HTTPException(400, "exec_id 不能为空")
    if not name:         raise HTTPException(400, "name 不能为空")
    if not dpi_password: raise HTTPException(400, "dpi_password 不能为空（需验证模板是否存在）")

    # 验证 exec_id 是否可读
    try:
        from .services.dpi_poller import get_dpi_token, _session, FLYTE_BASE
        token = await asyncio.to_thread(get_dpi_token, dpi_username, dpi_password)
        s = _session(token)
        r = await asyncio.to_thread(
            lambda: s.get(f"{FLYTE_BASE}/api/v1/executions/ebm-infra/dev/{exec_id}", timeout=15)
        )
        r.raise_for_status()
        spec = r.json().get("spec", {})
        workflow = (spec.get("launchPlan") or spec.get("launch_plan") or {}).get("name", "")
    except Exception as e:
        raise HTTPException(400, f"无法读取该 execution：{e}")

    tpls = _load_dpi_templates()
    import uuid as _uuid
    new_tpl = {
        "id": str(_uuid.uuid4())[:8],
        "name": name,
        "exec_id": exec_id,
        "workflow": workflow,
        "note": note,
        "created_at": now_cst().strftime("%Y-%m-%d"),
        "exec_url": f"https://dpi.dev.momenta.works/flyte-console/projects/ebm-infra/domains/dev/executions/{exec_id}",
    }
    tpls.append(new_tpl)
    _save_dpi_templates(tpls)
    return new_tpl


@app.post("/api/dpi/resolve-dataset")
async def dpi_resolve_dataset(request: Request):
    """解析用户输入（URL / 纯 ID / 名称）→ scenario_set_id"""
    body = await request.json()
    input_str = body.get("input", "").strip()
    if not input_str:
        raise HTTPException(400, "input 不能为空")
    try:
        from .services.dpi_poller import resolve_scenario_set_id
        result = await asyncio.to_thread(resolve_scenario_set_id, input_str)
        return result
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))


@app.delete("/api/dpi/templates/{tpl_id}")
async def dpi_templates_delete(tpl_id: str):
    if tpl_id == "default":
        raise HTTPException(400, "默认模板不能删除")
    tpls = _load_dpi_templates()
    tpls = [t for t in tpls if t["id"] != tpl_id]
    _save_dpi_templates(tpls)
    return {"ok": True}


@app.get("/api/pipeline/dpi-template")
async def pipeline_dpi_template():
    """返回 DPI 模板的默认参数（从上次成功的 execution 读取）。"""
    return {
        "template_exec_id": "afjr4kzx4g9jtgh9gv2b",
        "workflow": "ebm_infra.fst_cla_workflow.run_fst_cla_workflow",
        "template_url": "https://dpi.dev.momenta.works/flyte-console/projects/ebm-infra/domains/dev/executions/afjr4kzx4g9jtgh9gv2b",
        "params_hint": [
            {"key": "scenario_set_id", "label": "输入 Scenario Set ID", "editable": True},
            {"key": "name", "label": "任务名称（中文）", "editable": True},
            {"key": "scenario_set_name_prefix", "label": "输出集合前缀", "editable": True},
        ]
    }


@app.post("/api/pipeline/runs/{run_id}/poll")
async def pipeline_poll_now(run_id: int):
    """立即对指定批次执行一次 DPI 轮询（前端手动触发）。"""
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="not found")
    if not run.get("dpi_exec_id"):
        raise HTTPException(status_code=400, detail="该批次尚未填写 DPI execution ID")
    try:
        username = run.get("dpi_username", "junhao.niu")
        password = run.get("dpi_password", "")
        if not password:
            raise HTTPException(status_code=400, detail="请先填写 DPI 密码")
        dpi_token = await asyncio.to_thread(get_dpi_token, username, password)
        updates = await asyncio.to_thread(poll_once, run, dpi_token, username, password)
        if updates:
            from datetime import datetime as _dt
            updates["last_polled_at"] = _dt.now().strftime("%Y-%m-%d %H:%M:%S")
            updated = update_run(run_id, updates)
            return {"ok": True, "updates": updates, "run": updated}
        return {"ok": True, "updates": {}, "run": run}
    except Exception as e:
        logger.exception(f"poll_now failed for run {run_id}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/pipeline/runs/{run_id}/enable-poll")
async def pipeline_enable_poll(run_id: int, background_tasks: BackgroundTasks):
    """开启自动轮询（每2分钟后台扫一次），直到流水线完成。"""
    run = get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="not found")
    update_run(run_id, {"poll_enabled": 1})
    background_tasks.add_task(_auto_poll_loop, run_id)
    return {"ok": True}


async def _auto_poll_loop(run_id: int):
    """后台轮询循环，每2分钟一次，直到完成或出错停止。"""
    INTERVAL = 120  # 秒
    MAX_ROUNDS = 60  # 最多跑 2 小时
    round_n = 0
    dpi_token = None
    token_age = 0

    while round_n < MAX_ROUNDS:
        run = get_run(run_id)
        if not run or not run.get("poll_enabled"):
            break
        # 流水线完成了就停
        if run.get("dpi_exec_phase") in ("SUCCEEDED", "FAILED", "ABORTED"):
            if run.get("reinject_set_id"):
                update_run(run_id, {"poll_enabled": 0})
                break

        username = run.get("dpi_username", "junhao.niu")
        password = run.get("dpi_password", "")
        if not password:
            break

        try:
            # token 30分钟刷一次
            if dpi_token is None or round_n - token_age > 15:
                dpi_token = await asyncio.to_thread(get_dpi_token, username, password)
                token_age = round_n

            updates = await asyncio.to_thread(poll_once, run, dpi_token, username, password)
            if updates:
                updates["last_polled_at"] = now_cst().strftime("%Y-%m-%d %H:%M:%S")
                update_run(run_id, updates)
                logger.info(f"[poll] run {run_id} updated: {list(updates.keys())}")
        except Exception as e:
            logger.warning(f"[poll] run {run_id} round {round_n} error: {e}")

        round_n += 1
        await asyncio.sleep(INTERVAL)

    update_run(run_id, {"poll_enabled": 0})
    logger.info(f"[poll] run {run_id} loop ended after {round_n} rounds")


@app.get("/api/pipeline/runs")
async def pipeline_list():
    return list_runs()

@app.get("/api/pipeline/runs/{run_id}")
async def pipeline_get(run_id: int):
    r = get_run(run_id)
    if not r:
        raise HTTPException(status_code=404, detail="not found")
    return r

@app.post("/api/pipeline/runs")
async def pipeline_create(request: Request):
    data = await request.json()
    return create_run(data)

@app.patch("/api/pipeline/runs/{run_id}")
async def pipeline_update(run_id: int, request: Request):
    data = await request.json()
    r = update_run(run_id, data)
    if not r:
        raise HTTPException(status_code=404, detail="not found")
    return r

@app.delete("/api/pipeline/runs/{run_id}")
async def pipeline_delete(run_id: int):
    delete_run(run_id)
    return {"ok": True}


# ── FST 树写入 ───────────────────────────────────────────────────

@app.post("/api/fst/import")
async def fst_import(request: Request):
    body = await request.json()
    source   = body.get("source", "").strip()
    fst_url  = body.get("fst_url", "").strip()
    username = body.get("username", "junhao.niu").strip()
    password = body.get("password", "").strip()
    if not source:
        raise HTTPException(status_code=400, detail="source 不能为空")
    if not fst_url:
        raise HTTPException(status_code=400, detail="fst_url 不能为空")
    if not password:
        raise HTTPException(status_code=400, detail="密码不能为空")
    try:
        from .services.keycloak_auth import get_token as _kc_token
        kc_token = await asyncio.to_thread(_kc_token, username, password, True)
        result = await asyncio.to_thread(import_to_fst, source, fst_url, kc_token)
        return result
    except Exception as e:
        logger.exception("FST import failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── ALP Checker 任务提交 ─────────────────────────────────────────

@app.post("/api/alp/submit")
async def alp_submit(request: Request):
    body = await request.json()
    task_name = body.get("task_name", "").strip()
    raw_ids = body.get("scenario_set_ids", "").strip()
    username = body.get("username", "junhao.niu").strip()
    password = body.get("password", "").strip()
    priority = body.get("priority", "AUTO").strip()

    if not task_name:
        raise HTTPException(status_code=400, detail="task_name 不能为空")
    if not raw_ids:
        raise HTTPException(status_code=400, detail="scenario_set_ids 不能为空")
    if not password:
        raise HTTPException(status_code=400, detail="ALP 密码不能为空")

    ids = [x.strip() for x in raw_ids.replace(",", "\n").splitlines() if x.strip()]
    try:
        result = await asyncio.to_thread(
            alp_submit_checker, task_name, ids, username, password, priority
        )
        return result
    except Exception as e:
        logger.exception("ALP submit failed")
        raise HTTPException(status_code=500, detail=str(e))


# ── ALP 任务分析 ──────────────────────────────────────────────────

@app.post("/api/alp/analyze")
async def alp_analyze(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    task_input = body.get("task_input", "").strip()
    username = body.get("username", "junhao.niu").strip()
    password = body.get("password", "").strip()
    if not task_input:
        raise HTTPException(status_code=400, detail="task_input 不能为空")
    if not password:
        raise HTTPException(status_code=400, detail="ALP 密码不能为空")

    task_id = str(uuid.uuid4())
    tasks[task_id] = TaskStatus(task_id=task_id, status="pending", progress=0,
                                message="任务已提交...",
                                created_at=now_cst(), updated_at=now_cst())
    task_queues[task_id] = asyncio.Queue()
    background_tasks.add_task(_alp_analyze_task, task_id, task_input, username, password)
    return {"task_id": task_id}


async def _alp_analyze_task(task_id: str, task_input: str, username: str, password: str):
    queue = task_queues[task_id]
    loop = asyncio.get_event_loop()

    def on_progress(current: int, total: int, message: str):
        pct = current if total == 100 else (int(current / total * 100) if total else 0)
        tasks[task_id].progress = pct
        tasks[task_id].message = message
        tasks[task_id].updated_at = now_cst()
        loop.call_soon_threadsafe(queue.put_nowait,
                                  ProgressUpdate(type="progress", message=message, progress=pct))

    try:
        result = await asyncio.to_thread(
            alp_analyze_task, task_input, username, password, on_progress
        )
        tasks[task_id].status = "completed"
        tasks[task_id].progress = 100
        tasks[task_id].updated_at = now_cst()
        await queue.put(ProgressUpdate(type="complete", message="分析完成", progress=100,
                                       data=result))
    except Exception as e:
        logger.exception("ALP analyze task failed")
        tasks[task_id].status = "failed"
        tasks[task_id].error = str(e)
        tasks[task_id].updated_at = now_cst()
        await queue.put(ProgressUpdate(type="error", message=f"分析失败：{e}",
                                       progress=tasks[task_id].progress))


@app.post("/api/cla/bag-to-mviz")
async def cla_bag_to_mviz(request: ClaQueryRequest):
    """通过 bag 名批量查询 CLA Mviz 链接"""
    from .services.cla_service import query_bags_mviz
    if not request.bags:
        raise HTTPException(status_code=400, detail="bags 不能为空")
    bags = [b.strip() for b in request.bags if b.strip()]
    if not bags:
        raise HTTPException(status_code=400, detail="bags 不能为空")
    result = await asyncio.to_thread(query_bags_mviz, bags)
    return {"results": result}


# ── ETP 批量 Submit ────────────────────────────────────────────────

@app.post("/api/etp/preview")
async def etp_preview(request: Request):
    """解析 ETP URL，获取 batch 状态和问卷结构"""
    body = await request.json()
    task_url = body.get("task_url", "").strip()
    username = body.get("username", "junhao.niu").strip()
    password = body.get("password", "").strip()
    if not task_url: raise HTTPException(400, "task_url 不能为空")
    if not password:  raise HTTPException(400, "password 不能为空")
    try:
        from .services.etp_submit_service import _parse_url, etp_login, get_batch_info, get_questionnaire
        parsed = _parse_url(task_url)
        if not parsed["batch"]: raise HTTPException(400, "URL 中未找到 batch 参数")
        login   = await asyncio.to_thread(etp_login, username, password)
        info    = await asyncio.to_thread(get_batch_info, login["token"], parsed["batch"], parsed["tag_type"])
        qdata   = await asyncio.to_thread(get_questionnaire, login["token"], parsed["tag_type"])
        return {"batch": parsed["batch"], "tag_type": parsed["tag_type"],
                "batch_info": info, "questionnaire": qdata.get("questionnaire", {}),
                "tag_type_id": qdata.get("tag_type_id", "")}
    except HTTPException: raise
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/etp/auto-submit/stream")
async def etp_auto_submit_stream(request: Request):
    """ETP 批量提交 SSE 流 - 读取每条任务已有标注答案，原样提交"""
    body = await request.json()
    task_url = body.get("task_url", "").strip()
    username = body.get("username", "junhao.niu").strip()
    password = body.get("password", "").strip()
    if not task_url: raise HTTPException(400, "task_url 不能为空")
    if not password:  raise HTTPException(400, "password 不能为空")

    async def gen():
        from .services.etp_submit_service import batch_submit
        async for msg in batch_submit(task_url, username, password):
            yield f"data: {json.dumps(msg, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})
