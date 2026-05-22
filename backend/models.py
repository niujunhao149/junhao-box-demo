"""数据模型定义"""
from pydantic import BaseModel, Field
from typing import Optional, List
from datetime import datetime


class RoadtestRequest(BaseModel):
    """用户提交的路测整理请求"""
    plate: str = Field(..., example="IP5PM-4032", description="车牌号")
    start_time: str = Field(..., example="2026-03-01 00:00", description="开始时间 (YYYY-MM-DD HH:MM)")
    end_time: str = Field(..., example="2026-03-11 23:00", description="结束时间 (YYYY-MM-DD HH:MM)")


class EventDetail(BaseModel):
    """单条事件详情"""
    event_id: str
    name: str
    description: str
    package_version: str
    ess_url: str
    mviz_url: str


class TaskStatus(BaseModel):
    """任务执行状态"""
    task_id: str
    status: str  # "pending", "running", "completed", "failed"
    progress: int  # 0-100
    message: str
    events: Optional[List[EventDetail]] = None
    feishu_url: Optional[str] = None
    error: Optional[str] = None
    created_at: datetime
    updated_at: datetime


class SimBagRequest(BaseModel):
    """SimICT batch URL → 原包 & 仿真 Mviz 匹配请求"""
    batch_url: str = Field(..., description="SimICT batch 或 task URL，也可传纯 batch_id")


class FillSimRequest(BaseModel):
    """填写仿真结果到飞书项目 Case 请求"""
    batch_url: str = Field(..., description="SimICT batch URL")
    view_url: str = Field(..., description="飞书项目 Case 视图 URL")
    version: str = Field(..., description="版本号，如 .25")


class SimictTaskRequest(BaseModel):
    """创建 SimICT 仿真任务请求 - 支持多个链接，先创建 Scenario Set，再创建仿真"""
    links: List[str] = Field(..., description="Mviz / ESS 链接 / Event ID 列表（多个用换行分隔）")
    product: str = Field(..., description="产品类型：CP / AES / CMSR / ABSM / IHC / LSS / BSD / ESS / SLIF / DOW / ADB / NP / MNP")
    mf_package: Optional[str] = Field(None, description="MF 包名或 DevCar 包（CP/CI BUILD 模式需要）")
    scenario_name: Optional[str] = Field(None, description="Scenario Set 名称，不提供时自动生成")
    project: Optional[str] = Field(None, description="项目名，默认 default，可选 harz")
    repos: str = Field("CP_FST", description="FST 仓库名，默认 CP_FST")
    roi_start: Optional[str] = Field(None, description="ROI 开始时间（秒）")
    roi_end: Optional[str] = Field(None, description="ROI 结束时间（秒）")
    conservative: bool = Field(False, description="NP Conservative 模式")
    em_mode: bool = Field(False, description="NP EM 模式")
    use_ci_build: bool = Field(False, description="强制使用 CI BUILD 模式")


class ScenarioSetRequest(BaseModel):
    """创建 ESS Scenario Set 请求"""
    name: str = Field(..., description="Scenario Set 名称")
    links: List[str] = Field(..., description="Mviz / ESS 链接列表")
    repos: str = Field("CP_FST", description="仓库名，默认 CP_FST")


class CreateCaseRequest(BaseModel):
    """批量创建飞书 Case 请求"""
    links: List[str] = Field(..., description="Mviz / ESS 链接或 Event ID 列表")
    product: str = Field("UNP", description="产品线：UNP 或 APA")
    feishu_operator: str = Field(..., description="飞书用户名，如 junhao.niu")
    feishu_email: str = Field(..., description="飞书邮箱，如 junhao.niu@momenta.ai")


class PipelineRoadtestCaseRequest(BaseModel):
    """一键流程：路测整理 → 批量创建飞书 Case"""
    plate: str = Field(..., example="IP5PM-4032")
    start_time: str = Field(..., example="2026-03-01 00:00")
    end_time: str = Field(..., example="2026-03-11 23:00")
    product: str = Field("UNP", description="UNP 或 APA")
    feishu_operator: str = Field("junhao.niu")
    feishu_email: str = Field("junhao.niu@momenta.ai")


class PipelineRoadtestSimRequest(BaseModel):
    """一键流程：路测整理 → Scenario Set → 创建仿真任务"""
    plate: str = Field(...)
    start_time: str = Field(...)
    end_time: str = Field(...)
    product: str = Field(..., description="CP / NP / IHC / CMSR 等")
    mf_package: Optional[str] = Field(None)
    repos: str = Field("CP_FST")


class PipelineFillSimRequest(BaseModel):
    """一键流程：原包匹配仿真 → 填写仿真结果"""
    batch_url: str = Field(...)
    view_url: str = Field(...)
    version: str = Field(...)


class HarzExtractRequest(BaseModel):
    """Harz weekly release 版本信息提取请求"""
    release_url: str = Field(..., description="Harz weekly release 飞书文档 URL")


class HarzUpdateWikiRequest(BaseModel):
    """Harz 版本表更新请求"""
    release_url: str = Field(..., description="Harz weekly release 飞书文档 URL")
    doc_title: str = Field(..., description="文档标题")
    date: str = Field(..., description="发布日期，如 0326")
    st35_master: str = Field("", description="ST35 V920 .tgz")
    st35_weekly: str = Field("", description="ST35 A082.xx .tgz")
    st3_master: str = Field("", description="ST3 V920 .tgz")
    st3_weekly: str = Field("", description="ST3 A182.xx .tgz")
    devcar_7v: str = Field("", description="Devcar EBM 7v .tgz")


class MainlineCheckRequest(BaseModel):
    """主线版本更新检查请求"""
    ref_url_r6: str = Field(..., description="R6 参考文档 URL")
    ref_url_r7: str = Field("", description="R7 参考文档 URL")
    current_r6_dates: List[str] = Field(default_factory=list, description="当前已有的 R6 日期列表")
    current_r7_dates: List[str] = Field(default_factory=list, description="当前已有的 R7 日期列表")


class MainlineAddReleasesRequest(BaseModel):
    """主线版本写入请求"""
    r6: List[dict] = Field(default_factory=list)
    r7: List[dict] = Field(default_factory=list)


class FeedbackRequest(BaseModel):
    """用户提交的功能需求"""
    name: Optional[str] = Field(None, description="提交人（可匿名）")
    content: str = Field(..., description="需求内容")


class BugReportRequest(BaseModel):
    """用户提交的问题反馈"""
    name: Optional[str] = Field(None, description="提交人（可匿名）")
    content: str = Field(..., description="问题描述")


class DatasetCompareRequest(BaseModel):
    """数据集对比与运算请求"""
    input_sets: List[dict] = Field(
        ...,
        description="输入集合列表, 每项 {label, content}. content 为换行分隔的 Event Set ID / Scenario Set URL / Event Set URL"
    )
    operations: List[str] = Field(
        default=["compare"],
        description="运算列表: compare, union, intersection, difference, symmetric_difference"
    )
    difference_source: str = Field(
        "A",
        description="差集方向标签, 如 'A' 表示 A−B (需匹配 input_sets 中的 label)"
    )
    create_scenario_set: bool = Field(
        False,
        description="是否从运算结果创建新 Scenario Set"
    )
    scenario_set_name: Optional[str] = Field(
        None,
        description="新 Scenario Set 名称"
    )
    scenario_set_operation: str = Field(
        "union",
        description="从哪个运算结果创建: union / intersection / difference / symmetric_difference"
    )
    repos: str = Field(
        "CP_FST",
        description="仓库名, 默认 CP_FST"
    )
    scenario_set_owners: List[str] = Field(
        default=[],
        description="新 Scenario Set 的 owners 列表"
    )
    fetch_metadata: bool = Field(
        True,
        description="是否获取事件元数据 (车辆/版本/路线) 用于分布对比"
    )


class CreateScenarioSetRequest(BaseModel):
    """直接从 event_ids 创建 Scenario Set"""
    event_ids: List[str]
    name: str
    repos: str = "CP_FST"
    owners: List[str] = []


class ClaQueryRequest(BaseModel):
    """CLA bag → Mviz 查询请求"""
    bags: List[str]


class ProgressUpdate(BaseModel):
    """SSE 推送的进度消息"""
    type: str  # "progress", "event_count", "completed", "error"
    message: str
    progress: Optional[int] = None
    data: Optional[dict] = None
