# 汉字笔顺轨迹校验 API

基于 **FastAPI + SQLite** 的汉字书写校验服务：接收字符的参考笔画序列与用户书写轨迹，
按**笔画方向、起止点、笔顺**做归一化匹配，支持**可配置的分叉/拐角笔画容差**，
返回**首个不合格笔画、偏差原因与整体得分**。同一提交（`submission_id`）**幂等**。

## 快速开始

### Docker Compose（推荐）

```bash
docker compose up --build
```

启动后访问交互式文档：<http://localhost:8000/docs>（OpenAPI JSON：`/openapi.json`）。
SQLite 数据持久化在命名卷 `stroke-data` 中。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 运行测试

```bash
pip install -r requirements-dev.txt
pytest
```

## 使用流程

1. **登记参考字**（首次启动已自动写入演示字 `永` / `二` / `人`，可用 `SEED_DEMO_DATA=0` 关闭）：

```bash
curl -X PUT http://localhost:8000/references/十 \
  -H 'Content-Type: application/json' \
  -d '{"strokes": [[[10,50],[50,50],[90,50]], [[50,10],[50,90]]]}'
```

2. **提交书写轨迹校验**（`submission_id` 为幂等键，由客户端生成）：

```bash
curl -X POST http://localhost:8000/validate \
  -H 'Content-Type: application/json' \
  -d '{
    "submission_id": "hw-20260922-0001",
    "char": "十",
    "strokes": [[[12,51],[50,50],[88,49]], [[51,12],[50,88]]]
  }'
```

响应示例（笔顺写反的"二"）：

```json
{
  "submission_id": "demo-001",
  "char": "二",
  "passed": false,
  "score": 24.97,
  "first_failed_stroke": 1,
  "deviation_reasons": ["START_POINT_OFFSET", "END_POINT_OFFSET", "SHAPE_DEVIATION"],
  "expected_strokes": 2,
  "received_strokes": 2,
  "stroke_results": [
    {"index": 1, "score": 0.2497, "passed": false,
     "reasons": ["START_POINT_OFFSET", "END_POINT_OFFSET", "SHAPE_DEVIATION"],
     "metrics": {"start_distance": 0.5737, "end_distance": 0.5737,
                  "direction_error_deg": 0.0, "mean_shape_distance": 0.5745,
                  "max_shape_distance": 0.5848}}
  ],
  "replayed": false,
  "created_at": "2026-09-22T01:40:00.000+00:00"
}
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/health` | 健康检查 |
| `PUT` | `/references/{char}` | 登记/覆盖参考笔画（幂等） |
| `GET` | `/references/{char}` | 查询参考笔画 |
| `GET` | `/references` | 列出已登记参考字 |
| `DELETE` | `/references/{char}` | 删除参考字 |
| `POST` | `/validate` | 校验书写提交（幂等） |
| `GET` | `/submissions/{submission_id}` | 按幂等键查询历史结果 |

### 轨迹格式

- 每条笔画是有序点列 `[[x, y], ...]`，单点笔画（如"丶"）合法；
- `strokes` 数组顺序即**笔顺**，校验按序号一一对应比较；
- 坐标系任意（画布像素即可），服务内部做缩放/平移不变的对齐，**不要求**与参考字同坐标系。

### 偏差原因代码

| 代码 | 含义 |
| --- | --- |
| `REVERSED_STROKE` | 运笔方向写反（≥135°） |
| `WRONG_DIRECTION` | 运笔方向偏差超限 |
| `START_POINT_OFFSET` | 起笔位置偏移超限 |
| `END_POINT_OFFSET` | 收笔位置偏移超限 |
| `SHAPE_DEVIATION` | 笔画形状偏差超限 |
| `LOW_STROKE_SCORE` | 单笔综合得分低于合格线 |
| `MISSING_STROKE` | 缺少笔画 |
| `EXTRA_STROKE` | 多出笔画 |

### 匹配配置（`config`，均可选，按提交覆盖）

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `resample_points` | 48 | 每条笔画重采样点数 |
| `direction_tolerance_deg` | 50 | 运笔方向最大角度偏差（度） |
| `start_end_tolerance` | 0.18 | 起/终点最大偏移（归一化单位） |
| `shape_tolerance` | 0.15 | 形状平均偏差容忍度 |
| `shape_max_factor` | 2.5 | 单点最大偏差允许超出 `shape_tolerance` 的倍数 |
| `fork_angle_deg` | 55 | 判定拐角/分叉的最小转折角（度） |
| `fork_window` | 3 | 拐角两侧放宽的采样点数 |
| `fork_extra_tolerance` | 0.12 | **分叉/拐角处额外允许的偏差** |
| `alignment_inlier_threshold` | 0.12 | 对齐时判定离群笔画的残差阈值 |
| `pass_stroke_score` | 0.6 | 单笔合格分数线（0~1） |

"分叉笔画"指含转折/钩挑的复合笔画（如横折钩）。算法在参考笔画的拐角
（转折角 ≥ `fork_angle_deg`）及其邻域（`fork_window`）内，将形状偏差容忍度放宽
`fork_extra_tolerance`，以包容用户在转折处的圆转、顿笔差异；置 `0` 即严格匹配。

## 幂等语义

- `POST /validate` 以 `submission_id` 为幂等键，结果持久化于 SQLite；
- **同 id + 同载荷**：直接返回首个结果（`replayed: true`），不重复计算；
- **同 id + 不同载荷**：返回 `409 Conflict`（对请求体的规范化 SHA-256 校验）；
- `GET /submissions/{id}` 可随时重放历史结果。

## 匹配算法

1. **重采样**：每条笔画按弧长等距采样为 N 点（与书写速度、采样密度无关）；
2. **稳健对齐**：用共有笔画最小二乘估计用户→参考的相似变换（等比缩放 + 平移，
   不旋转），并剔除离群笔画后二次拟合——消除画布偏移/缩放差异，且漏笔或个别
   写偏的笔画不会扭曲其余笔画的位置判定；
3. **归一化**：统一套参考字的整字归一化变换（等比缩放到单位正方形）；
4. **逐笔比对**（按笔顺）：方向（整体位移夹角 + 分段切向余弦）、起点/终点偏移、
   形状偏差（分叉/拐角处按配置放宽）；
5. **评分**：单笔分 = 0.35·形状 + 0.25·方向 + 0.20·起点 + 0.20·终点；
   整字分 = 单笔分之和 ÷ max(应收, 实收) × 100（漏笔计 0 分、多笔摊薄）。

> 说明：仅两笔且其中一笔整体写偏时，"哪一笔偏了"在数学上不可区分，此时按
> 全局对齐结果判定，可能两笔都被标记——属预期行为。

## 环境变量

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `DATABASE_PATH` | `./strokes.db`（容器内 `/data/strokes.db`） | SQLite 文件路径 |
| `SEED_DEMO_DATA` | `1` | 参考字表为空时写入演示字（永/二/人），`0` 关闭 |

## 项目结构

```
app/
├── main.py       # FastAPI 路由与幂等处理
├── models.py     # Pydantic 请求/响应模型（含匹配配置）
├── matcher.py    # 对齐、逐笔比对与评分
├── geometry.py   # 重采样、归一化变换、拐角检测
├── db.py         # SQLite 持久化（WAL）
└── seed.py       # 演示参考字
tests/test_api.py # 端到端测试（16 例）
Dockerfile / docker-compose.yml / requirements*.txt
```
