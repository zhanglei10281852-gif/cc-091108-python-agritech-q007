# 农机轨迹核算资料

资料包含作业地块、机具幅宽、定位点和提升器状态。地理坐标使用 WGS84，终端序列号只在一次启动周期内递增，因此记录还带有 `boot_id`。

`reference/domain.json` 中地块边界首尾闭合。定位质量按厘米级固定解、浮点解和无效解区分，只有机具处于入土状态的轨迹才可能形成作业覆盖。

运行一致性检查：

```bash
python -m unittest discover -s tests
```

## 核算引擎（agrisettle）

针对"按在线时长付款失真"的异议，`agrisettle` 由轨迹还原有效作业覆盖，按面积出账：

- **航段还原**：无效定位与提升器抬起的点直接切断链条，相邻点超时（`max_gap_s`）或超距（`max_bridge_m`）不连成航段——短暂定位漂移不能凭插值跨过河沟；终端重启序号归零由 `(boot_id, sequence)` 联合主键区分，重启本身不断链。
- **覆盖核算**：航段按机具幅宽生成胶囊体，栅格化到按投影原点绝对对齐的网格，逐格分类——首次覆盖 / 必要重叠（时间窗内第二次覆盖）/ 重复碾压（更晚或更多次）/ 越界作业（田外）。同趟作业的航段接缝（`joint_suppression_s`）不重复计次。
- **幂等**：轨迹按主键去重，航段 id 与账单指纹由内容确定性派生；分批上传、重复上传、服务重启后得到有效面积相同。同主键不同内容的记录保留先到者并报告冲突。
- **边界版本**：边界更正版登记后，未签认的草稿账单自动按新边界重算（旧稿作废留痕），已签认账单保持冻结。
- **人工归类**：承包人可对争议航段提交带理由的归类（`submit_ruling`），只改分类标签，原始轨迹点永不修改；合作社签认（`sign_bill`）后账单与覆盖快照成为固定版本。
- **下钻**：`bill_report` → `bill_cells` → `cell_detail` → `segment_detail`，从账单金额逐级追到覆盖网格、覆盖事件、航段和原始轨迹点。

```python
from agrisettle import SettlementEngine

engine = SettlementEngine("settlement.json")          # 状态持久化，重启可恢复
engine.register_field("PLOT-88", boundary, revision=3)
engine.set_implement("PLOT-88", "ROTARY-6", 2.4)
engine.ingest_track("PLOT-88", points)                # 可分批、可重复
bill = engine.compute_bill("PLOT-88", unit_price_per_mu=38.0)
engine.sign_bill(bill["id"], "合作社李会计")           # 签认后冻结
```

完整流程演示（分批/重复上传、重启、四类核算、边界更正、人工归类、签认、下钻、幂等校验）：

```bash
python3 examples/demo.py
```
