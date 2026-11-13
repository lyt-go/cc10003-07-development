# 指标口径与分析实验平台

面向业务指标定义、口径管理、聚合查询、分群分析、A/B实验设计、分析方案管理与数据服务发布的数据分析工程平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/            每个业务模块一个页面
│   ├── src/api/              统一请求封装
│   ├── src/stores/           会话与筛选状态
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/          每个业务模块一组接口
│   ├── app/services/         业务规则与状态流转
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm install
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 指标口径 | `metric` | 指标定义 | 指标编号、指标名称、计算公式 |
| 聚合查询 | `queryboard` | 查询模板 | 模板编号、指标名称、维度字段 |
| 分群分析 | `segment` | 人群分群 | 分群编号、分群名称、筛选规则 |
| 实验设计 | `abtest` | 实验方案 | 方案编号、实验名称、对照组定义 |
| 分析方案 | `analysis` | 分析方案 | 方案编号、分析主题、数据集范围 |
| 看板配置 | `dashboard` | 看板页面 | 看板编号、看板名称、包含指标 |
| 数据服务 | `delivery` | 服务版本 | 服务编号、数据集名称、查询参数定义 |
| 漏斗分析 | `funnel` | 漏斗定义 | 漏斗编号、漏斗名称、步骤序列 |
| 同期群分析 | `cohort` | 同期群定义 | 群组编号、群组名称、划分事件 |
| 归因模型 | `attribution` | 归因规则 | 规则编号、归因类型、回溯窗口天 |
| 指标预警 | `alertconfig` | 预警规则 | 预警编号、关联指标、比较运算符 |
| 报表调度 | `reportsch` | 报表任务 | 任务编号、报表名称、包含指标 |
| 维度属性 | `dimprop` | 维度属性 | 属性编号、维度名称、属性名称 |
| 多维下钻 | `datadrill` | 下钻路径 | 路径编号、起始维度、下钻层级 |
| 预测模型 | `forecast` | 预测方案 | 方案编号、预测目标、算法名称 |
| 标签管理 | `tagmgr` | 标签定义 | 标签编号、标签名称、标签分类 |
| 洞察推送 | `insight` | 洞察报告 | 报告编号、报告标题、分析主题 |
| 数据验证 | `dataval` | 验证任务 | 任务编号、验证数据集、验证类型 |

## 标签版本驱动的分群重算

分群规模由「标签版本 + 筛选规则」共同决定，链路落在 `segment`、`tagmgr` 两个模块上：

- 标签发布新版本时（`POST /api/tagmgr/{id}/publish` 或 `发布标签` 动作），先写入版本快照
  （覆盖比例、对象偏移、覆盖对象数），再反查规则里引用该标签的分群：
  已使用当前版本的继续复用，版本落后的进入重算，仍处于待校验的等人工复核。
- 每个分群一次重算生成一个批次（`BATCH-SEG-YYYYMMDD-A`），把最终规模、覆盖对象数、
  最近计算时间、标签版本、规则版本写回同一份分群结果，并记录条件执行记录。
- 重算规模低于最小规模或高于最大规模时，分群停在 `规则校验中 / 待校验`，
  保留旧的最终规模供追溯，`生效分群` 动作会被拒绝，直到人工复核或重新计算通过。
- 分群级互斥保证同一分群并发刷新时至多一份更新生效，其余请求返回明确拒绝。
- 失败批次的检查点（失败条件序号 + 已筛出的对象集合）可用于续算：
  `POST /api/segment/batches/{批次号}/retry`，从受影响条件继续且按对象去重，不重复计入。
- 分群生效后标签侧可反查引用分群；每次标签发布都会在 `GET /api/tagmgr/audit`
  留下版本差异（旧值 → 新值）、影响分群与重算批次。

前端分群页支持按分群编号、状态、引用标签、所属批次组合过滤，
`跨页定位` 会返回目标所在页码并高亮该行，同时展开它的批次明细。
测试：`cd backend && .venv/bin/python -m unittest discover -s tests`。

## 约定

- 每个模块的前端页面在 `frontend/src/views/<模块>/index.vue`，后端接口在
  `backend/app/routers/<模块>.py`，业务规则在 `backend/app/services/<模块>.py`。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

领域：数据工程
子方向：数据分析与数据应用
