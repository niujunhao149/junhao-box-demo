# Junhao BOX 🦁

**你的一站式 CP / ADAS 效率百宝箱**

这是一个为 Momenta CP/ADAS 团队打造的自动化效率工具集，涵盖从路测数据采集、标注、仿真到评测的完整闭环。

---

## 🎯 核心功能

### 数据管理
- 🚗 **路测数据自动整理** — 从 ESS 抓取路测事件，10秒自动生成飞书文档
- 🎯 **创建 Scenario Set** — 批量数据集打包，支持并集/交集/差集运算
- 🌲 **写入 FST 树** — 批量导入事件到 ESS FST 评测集节点
- 📊 **数据集对比与运算** — 多个数据集重叠分析，一键创建新集合

### ALP / 标注流程
- 📤 **提交 ALP 任务** — 一键提交 cp_aes_tagger Checker 任务
- 🔬 **ALP 任务分析** — 批量分析所有 entity 的 category 分布
- 🐉 **AES CUTIN 数据一条龙** — 管理完整标注流水线（DPI → ALP → Sim → ETP → FST）

### 仿真相关
- 🔬 **原包匹配仿真** — 自动匹配原包与仿真 Mviz，生成对比表
- 🚀 **创建仿真任务** — 在 SimICT 平台创建仿真任务（CP/NP/ADAS）
- 📝 **填写仿真结果** — 批量将 Mviz 写入飞书 Case 解决方案

### 其他
- 📋 **批量创建飞书 Case** — 从链接批量创建测试 Case
- 📦 **每周发版速查** — 查阅各项目最新发版产物包（Harz / 主线 R6/R7）

---

## 🚀 技术栈

**后端：**
- FastAPI + Uvicorn
- SQLite（一条龙流水线数据）
- SSE（Server-Sent Events）实时推送

**前端：**
- Alpine.js（响应式状态管理）
- Tailwind CSS（现代化 UI）
- 单页应用（SPA）

**集成：**
- ESS / ALP / DPI / SimICT / ETP 全平台打通
- 飞书文档自动创建
- Keycloak 统一认证

---

## 📸 预览

> 注：本仓库为静态展示版本，不包含后端功能。

**主页**
- 工具卡片网格布局
- 一条龙流水线管理入口
- 每周发版速查

**一条龙流水线**
- 路测挖掘线：DPI → ALP → Sim → ETP → 回灌 → FST
- 日常回流线：ETP → FST
- 步骤状态实时追踪
- 自动轮询 + 分析

---

## 💡 设计理念

- **全自动化** — 减少重复劳动，10秒出结果
- **一站式** — 覆盖路测到仿真的完整闭环
- **半自动流水线** — 人工标注环节暂停，其余自动流转
- **实时反馈** — SSE 推送进度，不用刷新
- **数据持久化** — SQLite + JSON 文件，容器重启不丢失

---

## 🛠️ 本地部署（功能版）

需要 Momenta 内网环境。

```bash
# 克隆代码
git clone https://github.com/niujunhao149/junhao-box-demo
cd junhao-box-demo

# 安装依赖
pip install -r requirements.txt

# 配置环境变量（复制 .env.example 并填写）
cp .env.example .env

# 启动服务
uvicorn backend.main:app --host 0.0.0.0 --port 8001
```

访问 `http://localhost:8001`

---

## 📝 License

MIT

---

Made with ❤️ by junhao.niu | Powered by FastAPI + Alpine.js
