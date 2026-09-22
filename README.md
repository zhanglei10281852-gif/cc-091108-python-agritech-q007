# 农机轨迹核算资料

资料包含作业地块、机具幅宽、定位点和提升器状态。地理坐标使用 WGS84，终端序列号只在一次启动周期内递增，因此记录还带有 `boot_id`。

`reference/domain.json` 中地块边界首尾闭合。定位质量按厘米级固定解、浮点解和无效解区分，只有机具处于入土状态的轨迹才可能形成作业覆盖。

运行一致性检查：

```bash
python -m unittest discover -s tests
```

# 核算引擎 `agri_settle`

针对“按在线时长付款失真”的异议，引擎按地块多边形、机具幅宽、定位质量、
提升器状态和作业轨迹还原**有效覆盖**，并支撑账单全生命周期：

```
轨迹点 ──幂等接收──> 规范时序 ──构建航段──> 合并行程 ──栅格化──> 覆盖分类
                                                              │
账单金额 <── 分类面积 × 单价 <── 人工归类(带理由) <────────────┘
   │
   └──> 下钻:金额 → 覆盖分类 → 栅格单元 → 行程 → 原始定位点
```

## 数据怪癖的处理

- **断点续传 / 重复上传**：点的唯一身份是 `(device_id, boot_id, sequence)`，
  重复上传幂等去重；`ingest_status()` 返回每个启动周期已收到的序号区间，
  供续传比对。同一身份内容不一致时保留首条并记冲突，**原始点绝不改写**。
- **终端重启序号归零**：`boot_id` 区分启动周期，重启不撞号；规范时序按
  `(时间, boot_id, sequence)` 排序，与到达顺序无关。
- **地块边界修订**：`revision` 单调递增；更正版只自动重算**未签认**的账单，
  已签认账单连同边界版本、网格、金额一起冻结。
- **服务重启**：所有变更追加到 JSONL 事件日志，`Engine.restore()` 重放后
  指纹一致；分批、重发、重放均得到相同有效面积（见 `tests/test_idempotency.py`）。

## 覆盖分类规则

航段先经三道闸：时间断档(`max_gap_s`)、距离跳变(`max_segment_m`)、
**河沟屏障**（连线接触屏障即断轨，短暂漂移不能凭插值跨过河沟）；
连续共线的航段再合并为“行程”，行程是覆盖分类的最小单位。

| 分类 | 判定 | 计酬 |
| --- | --- | --- |
| 首次覆盖 `first` | 栅格单元首次被入土作业幅宽扫到（界内、非河沟） | ✓ |
| 必要重叠 `necessary_overlap` | 与早先平行行程横向重叠 ≤ `necessary_overlap_max_m`，且行程本身扫到新单元格 | ✓ |
| 重复碾压 `repeated_compaction` | 航向交叉（田头转弯）、横向重叠超限、或整段无新单元格 | ✗ |
| 越界作业 `out_of_bounds` | 幅宽扫到地块边界外（河沟内单元格不算越界，直接排除） | ✗ |

提升器抬起（道路转场）与无效解不产生覆盖；浮点解计入但标记 `low_confidence`。

## 人工归类与签认

承包人可对争议航段提交**带理由**的人工归类（按航段号或时间窗选择），
只允许在“必要重叠 / 重复碾压”之间裁定——首次覆盖与越界是几何事实，
不接受裁定；归类不改写任何原始点。合作社 `sign_bill()` 签认后账单冻结，
此后的轨迹、边界修订、归类都不再影响它。

## 用法

```python
from agri_settle import Engine

engine = Engine(log_path="events.jsonl")     # 事件日志支撑服务重启重放
engine.register_field("PLOT-88", boundary, revision=1)
engine.register_implement("ROTARY-6", working_width_m=2.4)
engine.register_barrier("PLOT-88", "RIVER-1", river_ring)
engine.upload_points("BEIDOU-07", track_points)   # 幂等,可分批/重发
engine.create_bill("BILL-0911", "PLOT-88", "ROTARY-6", rate_per_ha="900")
engine.submit_override("BILL-0911", {"time_range": [t0, t1]},
                       "necessary_overlap", reason="…", author="承包人")
engine.sign_bill("BILL-0911", signed_by="合作社", at="2026-09-12T09:30:00+08:00")
engine.drill_down("BILL-0911", cls="first")        # 金额 → 网格 → 轨迹
```

完整争议处理演示（在线 8 小时异议的还原、归类、签认、边界更正、重启重放）：

```bash
python3 examples/dispute_demo.py
```

核算阈值集中在 `Policy`（栅格边长、断档秒数、跳变米数、平行航向差、
必要重叠上限等），所有默认值显式可查，保证核算可解释、可复算。
