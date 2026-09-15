# IFC 建筑模型核查工具

读取 IFC 文件中的**墙体、门、窗、房间**，自动完成四类核查并导出报告：

1. **未闭合的墙**
   - 自由墙端：墙端头在容差范围内没有与任何墙体连接；
   - 墙段缺口：两段墙端头相对但没有搭接，量化缺口长度；
   - 房间围护缺口：逐房间检查边界，找出没有被墙 / 门覆盖的开口段。
2. **重复构件**：同类型构件形心重合（默认 80mm）且几何一致（体积比 / 轮廓 IoU），
   通过并查集聚类成组，输出每组的全部 GlobalId。
3. **房间净面积清单**：优先采用 `Qto_SpaceBaseQuantities.NetFloorArea`
   声明值，缺失时由房间平面几何计算；并校验声明值与几何值偏差（默认 >2% 警告），
   统计每个房间的门、窗数量与围护闭合状态。
4. **门窗规格清单（门窗表）**：按**楼层 + 房间**归并统计每类门 / 窗的
   类型、宽×高尺寸与数量；自动标出两类问题门窗——
   - **尺寸异常**：宽 / 高缺失或小于配置下限（默认门 600×1800mm、窗 400×400mm）；
   - **未归属**：没有归到任何房间的门窗（门位于两个房间边界时计入两侧）。
   尺寸优先取 IFC `OverallWidth/OverallHeight` 属性，缺失时按几何包围盒推断。

五组判定阈值（自由端 / 墙段缺口、房间围护缺口、重复构件、面积偏差、
门窗规格）均**可配置**：
内置标准/严格/宽松三套预设，也可用 JSON 配置文件或命令行 `--set` 单项调整，
每次报告（Excel「判定阈值」表、JSON、平面图标注、控制台）都会注明本次使用的阈值方案。

此外，管理员可把**核查项开关 + 阈值 + 放行条件**组合成**可命名、可版本化的
企业规则包**（`rulepack` 子命令），指定适用项目与阶段；发布后各项目核查时
自动按项目 / 阶段匹配规则包版本，报告标注所用规则包版本与指纹以便追溯，
详见下文「企业审查规则库」。规则包升版时用 `ruleswitch` 子命令对同一批模型
按新旧版本**重算试算**，区分规则调整与模型整改带来的问题变化，确认后联动
更新项目规则选择、放行门禁与趋势统计。

支持**多模型批量核查与项目质量看板**（`batch` 子命令）：一次纳入项目下
多个单体 IFC，按项目 / 单体 / 楼层汇总问题分布、净面积与门窗规格指标，
按批次留存结果形成趋势对比，并由可配置的放行门禁在质量不达标时阻断放行，
详见下文「多模型批量核查与项目质量看板」。

支持**多专业协同核查**（`coord` 子命令；`batch` 纳入建筑/结构/机电模型时
自动执行）：把建筑、结构、机电模型放在同一坐标系下比对，检测跨专业
**硬碰撞**与**预留洞口缺失 / 规格位置不符 / 洞口闲置**，冲突按专业
自动派给责任人，经「派单 → 整改 → 复核 → 回写建筑侧批次结论」闭环流转，
并与批次放行门禁联动，详见下文「多专业协同核查（建筑 / 结构 / 机电）」。

结果支持：

- **点击问题定位构件**：GUI 中点击问题列表，三维视图高亮对应构件并缩放到该位置；
  也可一键在 PyVista 交互窗口中打开（可旋转、缩放、点选）。
- **导出表格**：Excel（汇总 / 问题清单 / 房间净面积 / 重复构件 /
  门窗表 / 门窗明细 6 张表）、CSV、JSON。
- **导出标注图**：平面标注图（问题编号 + 红线标出围护缺口 +
  红 / 紫色虚线圈标尺寸异常 / 未归属门窗）与三维标注图。

## 安装

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
# GUI 需要系统 Python 带 tkinter（Debian/Ubuntu：sudo apt install python3-tk）
```

中文字体：项目 `fonts/` 目录可放置 `NotoSansSC.ttf`，程序会自动注册；
否则尝试使用系统已装的中文字体（微软雅黑 / 黑体 / 文泉驿等）。

## 命令行用法

```bash
# 核查并导出全部报告到 output/
python -m ifc_audit.cli audit path/to/model.ifc -o output/

# 切换严格预设（竣工审查）/ 宽松预设（方案阶段粗模）
python -m ifc_audit.cli audit model.ifc --profile strict

# 用配置文件调阈值；命令行再单项覆盖（长度 mm，面积偏差 %）
python -m ifc_audit.cli audit model.ifc --config thresholds.json \
    --set gap_min_len_mm=50 --set area_dev_warn_pct=1

# 生成带中文说明的配置模板（可基于 strict / loose 预设生成）
python -m ifc_audit.cli init-config thresholds.json --profile default

# CI 场景：存在错误级问题时退出码为 1
python -m ifc_audit.cli audit model.ifc --fail-on-error -q

# 图形界面（浏览文件、阈值设置、点击定位、导出）
python -m ifc_audit.cli gui

# 多专业协同核查（建筑/结构/机电碰撞与预留洞口，详见下文）
python -m ifc_audit.cli coord run 模型目录/ --project XX项目 \
    --owner struct=张工 --owner mep=李工
```

输出文件（以模型名 `sample` 为例）：

| 文件 | 内容 |
| --- | --- |
| `sample_核查报告.xlsx` | 汇总、问题清单、房间净面积、重复构件、门窗表、门窗明细六张表 |
| `sample_问题清单.csv` / `sample_房间净面积.csv` / `sample_门窗表.csv` | 对应表格的 CSV |
| `sample_标注平面图.png` | 平面图：墙/房间/门窗 + 问题编号 + 围护缺口红线 + 异常/未归属门窗圈标 |
| `sample_三维标注.png` | 三维轴测标注图（无 GPU 环境自动用 matplotlib 渲染） |
| `sample_结果.json` | 机器可读的完整结果（含门窗明细与门窗表） |

## 企业审查规则库

管理员把**核查项、阈值、放行条件**组合成**可命名、可版本化的规则包**，
指定**适用项目与阶段**；发布后各项目核查时自动按项目 / 阶段匹配规则包版本，
每份报告（单体 Excel / 标注图 / JSON、批次 Excel / 看板 / JSON）都标注
所用规则包的**名称@版本与内容指纹**，事后可凭版本精确追溯当时的核查口径。

规则库默认位于 `output/rule_library/`（`--rule-lib` 可改），目录结构：

```
<规则库>/
  index.json                         # 库索引
  drafts/<名称>.json                  # 可编辑草稿（改草稿不影响已发布版本）
  published/<名称>/<版本>.json         # 不可变发布快照（含 sha256 内容指纹）
```

### 管理规则包（rulepack 子命令）

```bash
# 1) 新建草稿（核查项全开；可指定适用项目/阶段，可重复；不给则适用全部）
python -m ifc_audit.cli rulepack init 住宅施工图审查 \
    --description "施工图阶段企业审查标准" \
    --project 花园小区一期 --project 花园路9号院 \
    --stage construction_drawing --profile default

# 2) 直接编辑草稿 JSON：checks 开关核查项、thresholds 阈值覆盖、
#    gate_rules 放行条件覆盖、applicability 项目/阶段
#    草稿位于 output/rule_library/drafts/住宅施工图审查.json

# 也可在库外生成带中文说明的草稿模板，编辑后用 publish --as-name 入库
python -m ifc_audit.cli rulepack template rules.json \
    --name 住宅施工图审查 --stage construction_drawing

# 3) 发布为不可变版本（语义化版本 主.次.修；同名同版本不可重复发布）
python -m ifc_audit.cli rulepack publish 住宅施工图审查 1.0.0 --by 张工
# 草稿再改不影响 1.0.0；调整后发布 1.1.0，老版本保留可追溯
python -m ifc_audit.cli rulepack publish 住宅施工图审查 1.1.0 --by 张工

# 列表 / 查看 / 废止（废止后不参与自动选择，快照仍保留）
python -m ifc_audit.cli rulepack list
python -m ifc_audit.cli rulepack show 住宅施工图审查@1.0.0
python -m ifc_audit.cli rulepack deprecate 住宅施工图审查 1.0.0
```

适用阶段取值：`scheme`（方案）/ `construction_drawing`（施工图）/
`submission`（提模审查）/ `completion`（竣工）。

草稿 JSON 结构（要点）：

```json
{
  "name": "住宅施工图审查",
  "description": "施工图阶段企业审查标准",
  "applicability": { "projects": ["花园小区一期"], "stages": ["construction_drawing"] },
  "checks": {
    "wall_closure": true,
    "room_envelope": true,
    "duplicate_element": true,
    "room_area": true,
    "opening_size": true,
    "opening_assignment": true
  },
  "threshold_profile": "default",
  "thresholds": { "gap_min_len_mm": 50, "area_dev_warn_pct": 1 },
  "gate_profile": "default",
  "gate_rules": { "unit_max_warnings": 20, "unit_max_open_rooms": 3 }
}
```

六个单专业核查项 + 两个多专业协同核查项：`wall_closure`（未闭合的墙）、
`room_envelope`（房间围护缺口）、`duplicate_element`（重复构件）、
`room_area`（房间净面积缺声明/偏差）、`opening_size`（门窗尺寸异常）、
`opening_assignment`（门窗未归属）、`mep_clash`（多专业硬碰撞）、
`reserved_opening`（预留洞口核对；仅多专业协同时产生问题）。
关闭某核查项后：相关问题不再产生、清单中不再标异常，对应的放行门禁规则
**自动不参与判定**（如关闭重复构件则重复组数门禁跳过，关闭任一核查项则
错误 / 警告总数门禁跳过，避免“不核查却仍按 0 阻断”）；房间净面积清单、
门窗表等**统计清单始终生成**，不受核查开关影响。

### 按规则包核查

```bash
# 批量：默认就会按 --project/--stage 从规则库自动选择适用的已发布包
python -m ifc_audit.cli batch ifc目录/ --project 花园小区一期 \
    --stage construction_drawing
# 规则库中没有适用包时自动回退内置预设；--no-rule-pack 可显式关闭自动选择

# 显式指定：库内名称（取最新版）、名称@版本、或发布快照 JSON 文件
python -m ifc_audit.cli batch ifc目录/ --project 花园小区一期 \
    --rule-pack 住宅施工图审查@1.0.0
python -m ifc_audit.cli batch ifc目录/ --project 花园小区一期 \
    --rule-pack /shared/rules/住宅施工图审查_1.0.0.json

# 单模型核查：显式 --use-rule-pack 自动选择，或 --rule-pack 指定
python -m ifc_audit.cli audit model.ifc --use-rule-pack \
    --project 花园小区一期 --stage construction_drawing
python -m ifc_audit.cli audit model.ifc --rule-pack 住宅施工图审查@1.0.0
```

自动选择按**相关度打分**：项目精确命中 > 全项目；阶段精确命中 > 全阶段；
同分取版本更高、发布更新者；已废止版本不参与。

为保证报告版本严格可追溯，**只要本次实际按规则包核查（批量自动选中或
显式 `--rule-pack`），就不允许同时指定**
`--profile/--config/--set/--gate-profile/--gate-config/--gate-set/--no-gate`
——这些参数不会被静默忽略，而是直接以退出码 2 报“配置冲突”并给出三种处理：
去掉冲突参数按规则包口径执行、批量加 `--no-rule-pack`（audit 去掉
`--use-rule-pack`）走不标注版本的命令行临时口径、或调整并发布新版本规则包。
只有自动选择**没有命中任何适用包、回退内置预设**时，这些命令行参数才照常生效
（该次报告 `rule_pack` 为空，本身即说明未使用企业规则包）。

### 报告中的版本标注

- 单体 Excel「汇总」与「判定阈值」表：规则包名称@版本、指纹、适用范围、
  发布时间、**每个核查项的启用/关闭状态**；平面标注图页脚打印规则包版本；
  `*_结果.json` 顶层新增 `rule_pack` 段；
- 批次 Excel「批次概览」「阈值与门禁」表：规则包版本/指纹/适用范围、
  核查项开关、每条门禁的「生效 / 不参与（核查项关闭）」状态；
  项目质量看板标题条、批次 JSON（`rule_pack` 与 `enabled_checks` 段）同样标注；
- 规则包快照带 **sha256 内容指纹**，快照被改动后载入会直接报错，
  确保发布版本不可篡改。

作为 Python 库调用：

```python
from ifc_audit.rule_packs import RulePackLibrary, materialize
from ifc_audit.batch import run_batch_with_rule_pack

lib = RulePackLibrary("output/rule_library")
pack = lib.select_for("花园小区一期", "construction_drawing")  # 或 load_published(名, 版本)
mat = materialize(pack)          # 物化为阈值 / 门禁 / 启用核查项 + 追溯引用
batch = run_batch_with_rule_pack(["ifc目录/"], mat, project="花园小区一期")
print(batch.rule_pack["id"], batch.rule_pack["content_hash"])
```

### 规则版本切换（试算 → 确认 → 联动）

规则包发布新版本后，项目不应直接改口径：先用 `ruleswitch compare`
对**上一批次同一批模型**分别按旧、新规则包**重算试算**，把
**规则调整**引起的问题变化（新增 / 消除 / 保留）与后续**模型整改**
引起的变化区分开；确认切换（`ruleswitch confirm`）后四处联动：
项目规则选择、放行门禁、趋势统计同步切到新版本，原始批次及原规则
版本全部保留供追溯。

```bash
# 1) 试算：对项目最新批次（--from-batch 可指定）的同一批模型，
#    按旧规则包（默认取原批次记录版本，--from-pack 可改）与新规则包各重算一次
python -m ifc_audit.cli ruleswitch compare --project 花园小区一期 \
    --to 住宅施工图审查@1.1.0 --by 张工

# 2) 查看试算记录与完整对比（新增/消除问题逐条清单、阈值/门禁差异）
python -m ifc_audit.cli ruleswitch list --project 花园小区一期
python -m ifc_audit.cli ruleswitch show --project 花园小区一期 RS20260915-101253-477

# 3) 确认切换：联动更新项目规则选择 / 放行门禁 / 趋势基线
python -m ifc_audit.cli ruleswitch confirm --project 花园小区一期 \
    --switch RS20260915-101253-477 --by 李工
```

试算报告内容（控制台 + `<项目>_规则切换试算_<编号>.json`）：

- **重算一致性**：旧规则重算与原始批次指标应完全一致；不一致说明
  磁盘上的模型相对原批次已被改动，差异会混入模型变化因素，给出提示；
- **规则调整影响**：同一批模型、仅规则口径不同下的问题
  新增 / 消除 / 保留（按核查项与单体归组，逐条列出），
  以及核查项开关、判定阈值、放行门禁的逐项差异；
- **放行门禁联动预览**：新旧口径下门禁结论（放行 / 阻断、
  新增未通过 / 转为通过的规则清单）。

确认切换后的联动行为：

1. **项目规则选择**：`<history>/<项目>/project_rule_binding.json`
   记录确认版本，后续 `batch` 自动按该版本核查（优先级高于规则库
   自动选择、低于显式 `--rule-pack`；`--no-rule-pack` 仍可退出企业口径）；
2. **放行门禁**：随绑定规则包物化，下一批次即按新口径判定；
3. **趋势统计**：新规则重算结果作为「规则切换基线」快照写入批次历史，
   其在时间线中的位置取**确认时刻**（切换生效点；批次号同时刷新，
   重算时刻与重算批次号保留在基线 `rule_switch` 元数据中），
   下一批次（模型整改后）的趋势增量相对该基线计算——即**模型整改**
   带来的变化，与试算得到的**规则调整**影响两条线互不混淆，
   批次汇总与批次 JSON 均标注基线类型。**延迟确认**（试算后、确认前
   又跑了旧口径批次）时，基线仍按确认时刻排在干扰批次之后，
   且趋势锚点选择只认与当前批次同口径的基线（补录 / 乱序的
   旧口径快照也不会混入趋势增量）；确认记录中会列出延迟期间的
   干扰批次号；
4. **追溯**：原始批次快照、原规则包发布版本均保持不变；
   试算记录（含两次重算的完整快照与逐条问题差异）归档在
   `<history>/<项目>/rule_switches/`，重复确认会被拒绝。


## 多模型批量核查与项目质量看板

一次纳入一个项目的**多个单体 IFC**（文件 / 目录 / 多路径混传均可），
按 **项目 → 单体 → 楼层** 三级汇总问题分布、净面积与门窗规格指标；
每批结果按项目**留存快照**，自动与上一批次形成趋势对比；
放行规则（质量门禁）不达标时以**退出码 3 阻断放行**，可直接挂 CI。

```bash
# 批量核查目录下全部单体（自动发现 .ifc/.ifcxml/.ifczip）
python -m ifc_audit.cli batch path/to/ifc目录/ --project 花园小区一期 \
    --label "v1首次提模" -o output/batch/

# 多个文件 / 目录混传
python -m ifc_audit.cli batch 1号楼.ifc 2号楼.ifc 地库/ --project XX项目

# 竣工审查用严格门禁；方案阶段用宽松门禁；--no-gate 只统计不阻断
python -m ifc_audit.cli batch ifc目录/ --project XX --gate-profile strict
python -m ifc_audit.cli batch ifc目录/ --project XX --no-gate

# 生成带中文说明的门禁配置模板，单项放行规则也可 --gate-set 覆盖
python -m ifc_audit.cli init-gate gate.json
python -m ifc_audit.cli batch ifc目录/ --project XX \
    --gate-config gate.json --gate-set unit_max_warnings=20

# 不导出每个单体的 Excel/平面图（只出批次报告，批量更快）
python -m ifc_audit.cli batch ifc目录/ --project XX --no-unit-reports

# 查看项目历次批次趋势（控制台），-o 同时导出趋势图
python -m ifc_audit.cli trend --project 花园小区一期 -o output/trend.png
```

退出码：`0`=核查完成且门禁通过（准予放行）；`2`=配置 / 参数错误；
`3`=**门禁不达标，已阻断放行**；核查阈值沿用 `--profile/--config/--set`。

> 不同目录下的**同名 IFC 会自动消歧**：单体名带上直接父目录
> （如 `A区/楼A.ifc`、`B区/楼A.ifc` → 单体 `A区-楼A`、`B区-楼A`），
> 父目录也相同则继续向上一级，保证批次内单体名、门禁判定、看板楼层行与
> 导出的单体报告文件名各自独立，不会串行结论或互相覆盖；单体的完整文件路径
> 仍记录在 Excel「单体汇总」与批次 JSON 中。

### 输出文件

| 文件 | 内容 |
| --- | --- |
| `<项目>_批次核查报告_<批次号>.xlsx` | 8 张表：批次概览、**放行判定**、单体汇总、楼层汇总、问题分布（单体×问题类型）、门窗规格汇总（跨单体门窗表）、趋势对比、阈值与门禁 |
| `<项目>_质量看板_<批次号>.png` | 项目质量看板：KPI 卡片、各单体问题堆叠、问题类型分布、净面积/不闭合房间、楼层问题、门窗异常/未归属、门窗规格构成、批次趋势与门禁未通过项清单 |
| `<项目>_批次结果_<批次号>.json` | 机器可读的完整批次结果（含门禁逐条判定与趋势增量） |
| `单体报告/<单体>_*` | 每个成功核查单体的单模型 Excel / CSV / 平面标注图（`--with-3d` 含三维图），格式与 `audit` 子命令一致 |
| `batch_history/<项目>/batches/*.json` | 按项目归档的**批次快照**，供后续批次趋势对比（`--history` 可改目录） |

### 放行门禁规则

门禁分两级逐规则判定，全部通过才放行（结果写入 Excel「放行判定」表与看板）：

- **单体级**（任一单体不达标即阻断）：错误数、警告数、每千 m² 错误数、
  围护不闭合房间数与占比、未归属门窗数、尺寸异常门窗数与占比、重复构件组数；
- **项目级**：错误总数、重复构件组总数、净面积为 0 房间数、
  最少纳入单体数（`min_units`，防止漏传文件）；
- **批次完整性**：存在无法解析的 IFC 默认直接阻断（`allow_failed_files=true` 可放开）。

| 门禁预设 | 适用场景 | 单体错误 | 不闭合房间占比 | 异常门窗占比 | 重复构件组 |
| --- | --- | --- | --- | --- | --- |
| `default` 标准 | 施工图模型常规放行 | 0 | 5% | 5% | 0 |
| `strict` 严格 | 竣工审查 | 0 | 2% | 2% | 0（警告数也受限） |
| `loose` 宽松 | 方案阶段粗模 | ≤5 | 10% | 10% | ≤3 |
| `none` 不设门禁 | 仅统计体检 | 不限制 | 不限制 | 不限制 | 不限制 |

上限类规则取 `-1` 即关闭该条；占比字段单位为 `%`，布尔字段取 `true/false`。

### 批次趋势

每次 `batch` 都会把精简快照留存到 `<history>/<项目名>/batches/`，
下次同项目核查时自动取上一批次对比：八项核心指标（问题总数 / 错误 / 警告 /
重复构件组 / 净面积 / 不闭合房间 / 未归属门窗 / 尺寸异常门窗）的增减量、
单体级错误变化、新增与缺失单体；看板趋势图按全部历史批次绘制，
阻断批次红底标注 `BLOCKED`。

## 多专业协同核查（建筑 / 结构 / 机电）

把**建筑、结构、机电**专业模型一起纳入（文件 / 目录 / 多路径混传），
在同一坐标系下完成两类跨专业核查：

1. **专业间硬碰撞**：机电管线（风管 / 水管 / 桥架 / 设备端子）与梁、柱、
   墙、板等围护构件几何相交（沿构件表面方向重叠不足
   `hard_clash_min_len_mm` 默认 100mm 的贴邻不报）；
2. **预留洞口核对**：管线穿越墙 / 板时，建筑/结构侧的
   `IfcOpeningElement` 是否存在、**位置**（默认容差 200mm）与
   **规格**（管线截面 + 每侧默认 50mm 安装余量，允许 20mm 负偏差）是否匹配；
   已预留但无任何管线使用的洞口单独标出（提示机电改路由或建筑封洞）。

检测结果按问题类型**自动派给责任专业**（可用 `--owner 专业=姓名` 指定到人）：

| 问题类型 | 默认严重程度 | 默认责任专业 |
| --- | --- | --- |
| 专业间硬碰撞 | 错误 | 机电（改路由；梁柱碰撞不得开洞） |
| 预留洞口缺失（穿越墙/板无洞） | 错误 | 建筑（墙洞）/ 结构（板洞按宿主归属） |
| 预留洞口规格 / 位置不符 | 警告 | 宿主所属专业（建筑 / 结构） |
| 预留洞口未被使用 | 警告 | 机电（核对路由） |

每条问题是一张工单，写入项目级**协同台账**（跨批次持久化，按
「问题类型 + 构件 GlobalId」指纹自动合单），状态机为：

```
待整改 open ──责任专业报整改──▶ 待复核 fixed ──发起专业复核通过──▶ 复核通过 verified
   ▲                                │
   └──────── 复核驳回 rejected ◀─────┘
重新核查未再检出：待复核 ─▶ 自动复核通过；待整改/驳回 ─▶ 已消除 cleared
已闭环问题再次出现 ─▶ 自动重开 open（回归问题，流转记录保留）
```

**整改时限与超时升级**：派单即按门禁设定的整改时限（默认 72h，严格 48h、
宽松 168h，`coord_fix_sla_hours` 可覆盖；0=不设时限）计算截止时间，覆盖
「派单 → 整改 → 驳回重派」整条线；每次核查台账时做幂等时限扫描：

- 待整改 / 驳回工单超过截止时间未整改 → **自动升级**：首次超期升级到
  责任专业负责人（L1），再超一个时限周期升级到项目协调 / 项目经理（L2），
  升级写入工单流转记录并在台账 / Excel / CSV / 看板标红；
- 待复核与已闭环工单不受时限约束；**复核驳回后重排整轮时限并清零升级标记**
  （历史升级记录保留）；已闭环问题回归重开同样重排时限；
- 超期未整改工单数计入协同放行门禁（`coord_max_overdue_active`，严格预设
  超期零容忍），任一超期即随协同门禁阻断批次放行；
- **历史台账兼容**：旧台账（无时限字段、schema v1）可直接加载，活动工单
  在下次核查 / 扫描时按当前时限**自补录时刻起算**补录截止时间（不追溯派单
  时间，避免一开启功能就把全部历史工单判成超期），并写 `sla_backfill`
  流转记录；已闭环工单不补。台账保存时升级为 schema v2。

复核通过后结论**回写建筑侧批次**，并与批次门禁联动：协同未闭环问题
不达标时批次以**退出码 3 阻断放行**。

### 专业模型的识别

文件名包含关键词即可自动识别：`结构/struct` → 结构，
`机电/mep/暖通/给排水/电气/hvac/piping` → 机电，
`建筑/arch` → 建筑；也可按文件内构件类型（梁/柱为结构，
管/风管/桥架为机电）投票判定。识别不准时用 `--discipline 单体名=专业`
显式指定（`arch` / `struct` / `mep`，可重复）。

### 独立协同核查（coord 子命令）

```bash
# 一次纳入三专业模型（文件名含 建筑/结构/机电），生成协同 Excel/工单 CSV/JSON
python -m ifc_audit.cli coord run 模型目录/ --project 花园小区一期 \
    --label "v1机电提模" -o output/coord/ \
    --owner struct=张工 --owner mep=李工 --owner arch=王工

# 责任人改派（可同时改责任专业）
python -m ifc_audit.cli coord assign COORD-0001 --project 花园小区一期 \
    --owner-discipline struct --owner 赵工 --by 协调人
# 责任专业整改完成（进入待复核）
python -m ifc_audit.cli coord fix COORD-0001 --project 花园小区一期 \
    --by 赵工 --note "风管改路由绕行框架梁"
# 发起专业复核（驳回必须给原因）
python -m ifc_audit.cli coord verify COORD-0001 --project 花园小区一期 --by 李工
python -m ifc_audit.cli coord reject COORD-0002 --project 花园小区一期 \
    --by 李工 --note "洞口仍偏 150mm，退回"
# 查台账（可按状态/责任专业过滤）
python -m ifc_audit.cli coord list --project 花园小区一期 --status active
```

退出码与批量一致：协同门禁不达标时退出码 3。台账默认保存在
`output/batch_history/<项目>/coordination_ledger.json`（`--ledger` 可改）。

协同检测参数与协同门禁可调整：

```bash
python -m ifc_audit.cli coord run 模型目录/ --project XX \
    --coord-set opening_pos_tol_mm=150      # 洞口位置容差
    --coord-set opening_extra_margin_mm=80  # 每侧安装余量
    --coord-gate-profile strict             # 全部清零、工单到人、48h 时限、超期零容忍
    --coord-gate-set coord_max_mismatch_active=5
    # 自定义整改时限与超期工单数上限
    --coord-gate-set coord_fix_sla_hours=24 --coord-gate-set coord_max_overdue_active=0
```

协同门禁预设：`default`（错误类零容忍，整改时限 72h）/ `strict`
（碰撞/缺洞/不符/闲置全部清零，工单必须指派到人，整改时限 48h、超期零容忍）
/ `loose`（允许 ≤3 项错误类，整改时限 168h）/ `none`（只统计不阻断）。
整改时限与超期门禁键：`coord_fix_sla_hours`（派单到整改完成时限，小时，
0=不设时限）、`coord_max_overdue_active`（超期未整改工单数上限，-1=不限制）。

### 与 batch 批次核查 / 门禁联动

`batch` 默认就会在识别出「机电 + 建筑/结构」多专业时**自动追加**协同核查：

```bash
python -m ifc_audit.cli batch 模型目录/ --project 花园小区一期 \
    --owner struct=张工 --owner mep=李工
# 关闭自动协同：--no-coord；识别不出多专业但必须协同：--require-coord
```

联动行为：

- 协同门禁逐条判定镜像进批次「放行判定」表（级别=多专业协同），
  任一协同规则不通过即整批阻断（退出码 3），与单专业质量门禁合取；
- 批次 Excel 增加「协同工单」「协同结论与门禁」两张表（含整改时限、截止时间、
  剩余/超期、升级级别列，超期工单深红标底），
  项目质量看板底部多专业协同面板显示整改时限与「超期未整改 / 已自动升级」数；
- 协同结论回写建筑侧：批次 JSON 顶层 `coordination.arch_writeback`
  含按建筑单体拆分的未闭环数，并导出
  `<项目>_多专业协同_<批次号>_建筑侧结论.json`，有建筑单体报告时
  同步写入 `单体报告/<建筑单体>_多专业协同结论_<批次号>.json`；
- 协同台账与批次快照同在 `<history>/<项目>/` 下，跨批次合单与自动闭环。

企业规则包新增两个核查项：`mep_clash`（专业间硬碰撞，含缺洞门禁）与
`reserved_opening`（预留洞口核对）；关闭后对应协同门禁规则自动不参与判定，
报告「阈值与门禁」表标注生效状态。

### 协同输出文件

| 文件 | 内容 |
| --- | --- |
| `<项目>_多专业协同_<批次号>.xlsx` | 协同概览（含整改时限/超期/升级统计）/ 碰撞与洞口工单（含时限状态、截止时间、剩余超期、升级级别，按状态与超期着色可筛选）/ 专业模型清单 / 判定参数与门禁 |
| `<项目>_多专业协同_<批次号>_工单清单.csv` | 全部工单（坐标、责任专业/人、构件 GlobalId、整改时限/截止/超期/升级、整改/复核记录） |
| `<项目>_多专业协同_<批次号>.json` | 机器可读完整结果 |
| `<项目>_多专业协同_<批次号>_建筑侧结论.json` | 回写建筑侧的批次结论（含按建筑单体拆分） |
| `batch_history/<项目>/coordination_ledger.json` | 项目协同台账（跨批次工单与流转记录） |

Python API：

```python
from ifc_audit.coordination import run_coordination
from ifc_audit import coordination_report

result = run_coordination(
    ["arch.ifc", "struct.ifc", "mep.ifc"],
    project="花园小区一期", label="v1",
    owners={"struct": "张工", "mep": "李工", "arch": "王工"},
    ledger_path="output/batch_history/花园小区一期/coordination_ledger.json")
print(result.gate_passed)
for i in result.issues:
    if i.active:
        print(i.issue_id, i.title, i.owner_discipline, i.owner, i.status)
coordination_report.export_all(result, "output/coord/")
```

生成三专业样例（含 1 碰撞 / 1 缺洞 / 1 洞口过小 / 1 闲置洞口）：

```bash
python tools/make_sample_coordination.py output/coord_sample
python -m ifc_audit.cli coord run output/coord_sample --project 协同样例
```

## 协同问题闭环（设计 / 结构 / 机电统一工单）

`coord` 只管多专业碰撞与预留洞口；**协同问题闭环模块（`collab`）**在其上把
四类来源的问题统一成一张跨专业工单台账：

| 来源 | 标识 | 问题 |
| --- | --- | --- |
| 批量审查 | `audit` | 未闭合墙、重复构件、房间净面积、门窗规格等单体问题 |
| 多专业协同 | `coord` | 硬碰撞、预留洞口缺失 / 不符 / 闲置（沿用协同指纹合单） |
| 规则校验 | `rule` | 批次 / 协同门禁阻断、规则口径校验问题 |
| 人工登记 | `manual` | 会审、现场问题（**不被重新核查自动消除**） |

`batch` 默认在每次核查后自动纳管（`--no-collab` 关闭），按项目把台账存在
`batch_history/<项目>/collab_ledger.json`。工单走
**派单 → 整改（可回写）→ 复核通过 / 驳回 → 关闭** 主线；跨批次按稳定指纹
合单，重新核查消失自动闭环（待复核自动通过、待整改标已消除），闭环后再现
自动**回归重开**；整改时限支持临期提醒、超期自动升级（专业负责人 → 项目协调）。

### 角色与权限（RBAC）

| 角色 | 权限 |
| --- | --- |
| `coordinator` 项目协调 | 全部操作、维护名册、管理门禁 |
| `design_lead` / `struct_lead` / `mep_lead` 专业负责人 | 本专业问题的登记、派单、整改、复核、关闭 |
| `responsible` 责任人 | 仅整改 / 回写**本人名下**工单 |
| `reviewer` 复核人 | 复核通过、驳回 |
| `viewer` 只读 | 查看与接收通知 |

责任人缺失时自动回落到该专业负责人；改派给只读成员会自动升为责任人角色。
未登记进名册的操作人默认按项目协调处理（向后兼容）。系统自动动作以
`系统` 身份执行，不做权限拦截。

```bash
# 维护名册（仅项目协调）
python -m ifc_audit.cli collab user 王协调 --project X --by 王协调 --role coordinator
python -m ifc_audit.cli collab user 张结  --project X --by 王协调 --role struct_lead --discipline struct
python -m ifc_audit.cli collab user 李机  --project X --by 王协调 --role mep_lead   --discipline mep
python -m ifc_audit.cli collab user 王复  --project X --by 王协调 --role reviewer
python -m ifc_audit.cli collab users --project X          # 查看名册
```

### 分派、跨模型定位与状态流转

```bash
# 查看 / 过滤工单（--source audit/coord/rule/manual，--status，--discipline，--mine）
python -m ifc_audit.cli collab list --project X
# 详情与跨模型定位（按专业/单体/文件分组列出构件 GlobalId，供 GUI 高亮）
python -m ifc_audit.cli collab show   COLL-0001 --project X
python -m ifc_audit.cli collab locate COLL-0001 --project X

# 人工登记会审 / 现场问题（可关联多个跨专业构件 GlobalId）
python -m ifc_audit.cli collab open "管综净高不足" --project X --by 王协调 \
    --owner-discipline mep --severity error --unit 1号楼 --storey 1F \
    --gid DUCT-1 --gid BEAM-2 --sla 48

# 派单 / 改派
python -m ifc_audit.cli collab assign COLL-0001 --project X --by 王协调 \
    --owner 李机 --owner-discipline mep
# 整改回写（回填整改说明与整改后构件，不改状态）与报整改
python -m ifc_audit.cli collab writeback COLL-0001 --project X --by 李机 \
    --note "风管上翻 300mm，净高满足" --gid DUCT-1-NEW
python -m ifc_audit.cli collab fix      COLL-0001 --project X --by 李机 --note "已整改"
# 复核通过 / 驳回（驳回必须填原因，并重排整改时限）
python -m ifc_audit.cli collab verify   COLL-0001 --project X --by 王复
python -m ifc_audit.cli collab reject   COLL-0001 --project X --by 王复 --note "仍有净距不足" --sla 24
# 人工关闭（会审销项 / 设计豁免，必须填原因）
python -m ifc_audit.cli collab close    COLL-0002 --project X --by 王协调 --reason "会审纪要#3 设计豁免"
```

### 通知与报告汇总

派单、改派、临期、超期、升级、报整改、驳回、闭环、门禁阻断等事件会按
**责任人 + 专业负责人（升级到项目协调）**投递到台账内置通知中心，成员可退订。

```bash
python -m ifc_audit.cli collab notifications 李机 --project X            # 未读
python -m ifc_audit.cli collab notifications 李机 --project X --all      # 含已读
python -m ifc_audit.cli collab notifications 李机 --project X --read-all
```

报告（Excel 四张表：闭环概览 / 工单台账 / 专业楼层汇总 / 名册与权限；
另出工单 CSV、JSON、整改回写 JSON）：

```bash
python -m ifc_audit.cli collab report --project X \
    -o output/collab/ --gate-profile strict
```

闭环门禁与批次放行联动（`--collab-gate-profile`）。为避免同一批问题被多套
门禁重复执法，三类门禁分工不同：

- **质量门禁**（`--gate-profile`）：单体 / 项目的原始问题数量；
- **多专业协同门禁**（`--coord-gate-profile`）：未闭环碰撞 / 洞口数量；
- **协同闭环门禁**（`--collab-gate-profile`）：只管闭环流程治理——
  `default` 仅卡**超期未整改**（问题数量交给上面两类门禁，不重复阻断）；
  `strict` 才在竣工阶段要求全部清零、工单到人、回写齐全、超期零容忍；
  另有 `loose` / `none`。

`batch` 结束时把三类门禁合并成**一条放行结论**（各自失败项分类列出，不再
重复打印多份「阻断放行」），任一未通过即以退出码 3 阻断；`--no-gate`
是整体逃生口，同时不阻断质量、协同与闭环门禁。

闭环输出文件：

| 文件 | 内容 |
| --- | --- |
| `<项目>_协同闭环_<批次号>.xlsx` | 闭环概览 / 工单台账（状态与超期着色）/ 专业楼层汇总 / 名册与权限门禁 |
| `<项目>_协同闭环_<批次号>_工单台账.csv` | 全部工单（来源、坐标、跨模型构件/文件、时限、整改回写、来源单号等） |
| `<项目>_协同闭环_<批次号>.json` | 台账 + 汇总 + 门禁判定 + 整改回写 |
| `<项目>_协同闭环_<批次号>_整改回写.json` | 回写单体 / 批次的闭环结论（按单体拆分） |
| `batch_history/<项目>/collab_ledger.json` | 项目闭环台账（工单 / 名册 / 通知，跨批次持久化） |

Python API：

```python
from ifc_audit.collab import (
    CollabLedger, ingest_batch, open_manual_ticket, assign_ticket,
    fix_ticket, verify_ticket, evaluate_collab_gate, for_gate_profile,
    collab_summary)
from ifc_audit import collab_report

path = "output/batch_history/花园小区一期/collab_ledger.json"
ledger = CollabLedger.load_or_new(path, "花园小区一期")
ingest_batch(ledger, batch, sla_hours=72)          # 批量审查 + 协同问题纳管
ok, rules = evaluate_collab_gate(ledger, for_gate_profile("strict"))
print(collab_summary(ledger)["by_status"])
collab_report.export_all(ledger, "output/collab/", gate_rules=rules,
                         gate_passed=ok, batch_id=batch.batch_id)
ledger.save(path)
```

## 图形界面

`python -m ifc_audit.cli gui` 打开窗口：

- 顶部选择 IFC 文件并执行核查；
- 左侧「问题清单」按严重程度着色（红=错误 / 黄=警告 / 蓝=提示），
  点击任意一条，右侧三维视图高亮关联构件并缩放定位，下方显示详情与 GlobalId；
- 「房间净面积」标签页给出每个房间的面积、来源、门窗数与闭合状态，
  点击可在三维中定位房间；
- 「门窗表」标签页按楼层 / 房间 / 类型 / 宽×高 汇总数量，
  橙底行为尺寸异常、灰底行为未归属房间的门窗，点击可在三维中定位；
- 「在 PyVista 中打开」启动独立的 PyVista 交互窗口（与 Tk 主循环隔离，
  避免 VTK/Tk 冲突），可旋转缩放查看；
- 「导出到目录…」保存全部表格与标注图。

## 作为 Python 库调用

```python
from ifc_audit.pipeline import audit_ifc
from ifc_audit import report

model = audit_ifc("model.ifc")
print(model.summary())
for issue in model.issues:
    print(issue.issue_id, issue.title, issue.global_ids)
for room in model.rooms:
    print(room.name, room.net_area, "m²", "闭合" if room.enclosed else "有缺口")
for row in model.opening_schedule:
    print(row.storey, row.room_name, "门" if row.kind == "door" else "窗",
          row.type_name, row.width, "×", row.height, "×", row.count,
          row.notes)

report.export_excel(model, "out/报告.xlsx")
report.export_annotated_plan(model, "out/标注图.png")
```

批量核查 / 看板 / 门禁（Python API）：

```python
from ifc_audit.batch import run_batch_with_config, attach_trend, save_batch_snapshot
from ifc_audit import batch_report

batch = run_batch_with_config(
    ["ifc/1号楼.ifc", "ifc/2号楼.ifc"],
    project="花园小区一期", label="v1提模",
    threshold_profile="default",          # 核查阈值：与 audit 一致
    gate_profile="default",               # 放行门禁
    gate_overrides={"unit_max_warnings": 20})

for u in batch.units:                     # 单体汇总
    print(u.name, u.errors, "错误", u.total_net_area, "m²",
          "不闭合房间", u.rooms_open, "异常门窗", u.opening_anomaly)
for s in batch.storeys:                   # 单体×楼层汇总
    print(s.unit, s.storey, s.net_area, s.errors)

print(batch.gate_passed)                  # True=放行，False=阻断
for r in batch.gate_results:              # 逐条门禁判定
    if not r.passed:
        print("[阻断]", r.scope, r.message)

attach_trend(batch, "output/batch_history")   # 附加与上一批次的趋势对比
save_batch_snapshot(batch, "output/batch_history")
batch_report.export_batch_excel(batch, "out/批次报告.xlsx")
batch_report.export_dashboard(batch, "out/质量看板.png")
batch_report.export_batch_json(batch, "out/批次结果.json")
```

三维定位（桌面环境）：

```python
from ifc_audit.viewer import Viewer3D

viewer = Viewer3D(model)
viewer.locate(issue.global_ids)   # 高亮并把相机对准构件
viewer.run()
```

## 判定阈值配置

五组判定的全部阈值集中在 `ifc_audit/thresholds.py`，调整方式有三种，
**优先级从低到高**：内置预设 → 配置文件 → 命令行单项覆盖。

### 1. 内置预设

| 预设 | 适用场景 | 自由端容差 | 缺口聚类 | 围护缺口下限 | 重复形心距 | 体积比 / IoU | 面积偏差 | 门窗尺寸下限 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `default` 标准 | 常规施工图模型 | 50mm | 300mm | 100mm | 80mm | 0.85 / 0.70 | 2% | 门 600×1800，窗 400×400 |
| `strict` 严格 | 高精度模型 / 竣工审查 | 30mm | 200mm | 50mm | 50mm | 0.90 / 0.80 | 1% | 门 700×2000，窗 500×500 |
| `loose` 宽松 | 方案阶段 / 粗模 | 100mm | 500mm | 200mm | 150mm | 0.80 / 0.60 | 5% | 门 500×1500，窗 300×300 |

### 2. JSON 配置文件

```bash
python -m ifc_audit.cli init-config thresholds.json   # 生成带中文说明的模板
python -m ifc_audit.cli audit model.ifc --config thresholds.json
```

文件中 `profile` 指定基准预设，其余键逐项覆盖；以 `_` 开头的键为注释，
会被忽略。用户面单位：长度 **毫米（mm）**，面积偏差 **百分比（%）**，
体积比 / IoU 为 0~1 小数：

```json
{
  "profile": "default",
  "gap_min_len_mm": 50,
  "area_dev_warn_pct": 1,
  "dup_iou": 0.80
}
```

### 3. 命令行单项覆盖

`--set key=value` 可重复，优先级最高；非法键名或越界值会报错并以退出码 2 终止：

```bash
python -m ifc_audit.cli audit model.ifc \
    --profile strict --set dup_iou=0.95 --set free_end_tol_mm=20
```

### 可配置键一览

| 配置键 | 含义 | 单位 | 分组 |
| --- | --- | --- | --- |
| `free_end_tol_mm` | 墙端头伸入其它墙体的判定容差 | mm | 自由端 |
| `endpoint_merge_tol_mm` | 邻近自由端聚类为「墙段缺口」的容差 | mm | 墙段缺口 |
| `gap_min_len_mm` | 房间围护缺口最小上报长度 | mm | 围护缺口 |
| `barrier_buffer_mm` | 围护覆盖外扩容差（毫米级建模误差吸收） | mm | 围护缺口 |
| `dup_centroid_tol_mm` | 重复构件形心距离 | mm | 重复构件 |
| `dup_vol_ratio` | 重复构件体积相似度（小/大） | 0~1 | 重复构件 |
| `dup_iou` | 重复构件平面轮廓 IoU | 0~1 | 重复构件 |
| `area_dev_warn_pct` | 声明面积与几何面积偏差警告线 | % | 面积偏差 |
| `door_min_width_mm` | 门最小宽度（小于即标尺寸异常） | mm | 门窗规格 |
| `door_min_height_mm` | 门最小高度 | mm | 门窗规格 |
| `win_min_width_mm` | 窗最小宽度 | mm | 门窗规格 |
| `win_min_height_mm` | 窗最小高度 | mm | 门窗规格 |

### 报告中注明本次阈值

- Excel 汇总表新增「判定阈值方案」行，并新增 **「判定阈值」工作表**
  （分组 / 判定项 / 本次取值 / 配置键）；
- `*_结果.json` 的 `thresholds` 段记录完整取值与来源（预设名、配置文件路径、覆盖项）；
- Excel **「门窗表 / 门窗明细」**工作表与 `*_门窗表.csv`、JSON `openings`
  段给出按楼层 / 房间归并的门窗规格统计，异常行橙底、未归属行灰底；
- 平面标注图页脚、控制台汇总均打印阈值方案说明；
- 每条问题详情使用**本次实际阈值**描述（如「上报下限 50mm」「偏差超过警告线 1%」）。

作为 Python 库调用时，可直接传阈值对象（内部单位为米/比例）：

```python
from ifc_audit.pipeline import audit_ifc_with_config, audit_ifc
from ifc_audit.thresholds import resolve

# 方式一：与 CLI 一致的解析（预设 + 配置文件 + 用户单位覆盖）
model = audit_ifc_with_config("model.ifc",
                              profile="strict",
                              config_path="thresholds.json",
                              overrides={"gap_min_len_mm": 50})

# 方式二：自行解析后传入
th, prov = resolve("default", overrides={"free_end_tol_mm": 20})
model = audit_ifc("model.ifc", thresholds=th, provenance=prov)
```

GUI 中点击工具栏「阈值设置…」可切换预设、载入配置文件或逐项编辑，
下次「开始核查」生效。

## 核查方法说明

- **几何提取**：优先解析参数化 `IfcExtrudedAreaSolid` 的二维轮廓
  （矩形 / 任意闭合曲线 / 圆形，支持挤出方向与 RefDirection），
  得到轴对齐的精确平面轮廓，避免 BRep 三角化在顶盖附近产生斜面瑕疵；
  BRep / 曲面等表示回退到「21 个高度水平切片 + shapely polygonize」。
- **墙体围护**：取门楣上方的完整墙身截面（参数化轮廓或最大面积切片），
  与门扇凸包联合后，对每个房间的平面边界做 `difference`；
  未被覆盖且长度超过 100mm 的线段即围护缺口。窗不参与围护（窗台以上为采光面）。
- **自由端 / 缺口**：用 PCA 从墙轮廓求中轴线与端点，
  检查端点是否进入其它墙体 50mm 范围；再把邻近的自由端聚类，
  同一位置出现 ≥2 个不同墙的端头时判为「墙段缺口」，否则为「自由墙端」。
- **重复构件**：同类构件两两比较，形心距 < 80mm、体积比 ≥ 0.85、
  封闭轮廓 IoU ≥ 0.70 即判重，并查集聚类成组。
- **门窗表**：门 / 窗宽高优先取 IFC `OverallWidth/OverallHeight`
  （单位随项目长度单位换算为米），属性缺失时取几何包围盒——
  水平长边为洞口宽度、竖直方向为高度；类型优先取 `ObjectType`，
  其次映射 `PredefinedType`（平开门 / 推拉门 / 天窗等），缺省为“门 / 窗”。
  按 楼层 + 归属房间 + 类型 + 宽×高 归并计数；归属规则与房间净面积清单
  一致（形心落在房间内或距边界 ≤ 归属距离），跨房间门计入两侧，
  没有任何房间接收的门窗单独列为「（未归属房间）」。

判定阈值集中在 `ifc_audit/thresholds.py`，支持内置预设（标准/严格/宽松）、
JSON 配置文件与命令行 `--set` 三种方式调整，详见上文「判定阈值配置」。
每次报告都会注明本次使用的阈值方案。

## 生成自带已知问题的样例模型

```bash
python tools/make_sample_ifc.py output/sample.ifc
python -m ifc_audit.cli audit output/sample.ifc -o output/
```

样例中人为注入：1 组重复墙、1 组重复门、1 个室内自由墙垛（2 个自由端）、
1 处 150mm 墙段缺口与对应房间围护缺口、1 个面积偏差房间、1 个无声明面积房间、
1 樘 300mm 宽异常小窗、1 樘游离门与 1 樘游离窗（未归属任何房间）。

可用多个样例 IFC 体验批量核查与门禁阻断（会退出码 3）：

```bash
python tools/make_sample_ifc.py /tmp/proj/1号楼.ifc
python tools/make_sample_ifc.py /tmp/proj/2号楼.ifc
python -m ifc_audit.cli batch /tmp/proj --project 示例项目
```

## 模块结构

```
ifc_audit/
  units.py       单位换算（项目长度单位 -> 米）
  geometry.py    网格切片、轮廓提取、中轴线/厚度
  analytic.py    参数化 IfcExtrudedAreaSolid 轮廓解析
  extract.py     IFC 提取墙/门/窗/房间
  thresholds.py  判定阈值：预设 / 配置文件 / 命令行覆盖
  gate.py        放行门禁：规则 / 预设 / 配置解析 / 模板导出
  rule_packs.py  企业审查规则库：规则包草稿 / 发布版本化 / 适用范围选择 / 物化
  rule_switch.py 规则版本切换：同一批模型新旧规则包重算试算、确认联动与项目规则绑定
  checks.py      重复构件、自由端、墙段缺口、房间围护
  rooms.py       房间净面积清单
  openings.py    门窗规格清单（门窗表 / 异常 / 未归属）
  coordination_model.py 多专业协同数据模型（专业构件/工单/台账/批次结论）
  coordination.py 多专业提取、碰撞与预留洞口检测、责任分派、台账流转、协同门禁
  coordination_report.py 协同 Excel / 工单 CSV / JSON / 建筑侧回写
  collab_model.py 协同问题闭环数据模型（统一工单/名册 RBAC/通知/台账）
  collab.py      多源纳管、分派、跨模型定位、状态流转、整改回写、通知权限、闭环门禁
  collab_report.py 闭环 Excel（概览/台账/专业楼层/名册）/ CSV / JSON / 回写
  batch.py       多模型批量核查、项目/单体/楼层聚合、门禁判定、协同联动、批次快照与趋势
  batch_report.py 批次 Excel / 项目质量看板 PNG / 批次 JSON / 趋势图
  report.py      Excel / CSV / 平面标注图
  viewer.py      PyVista 三维查看器 + matplotlib 三维回退
  viewer_win.py  独立进程 PyVista 窗口
  gui.py         Tkinter 图形界面
  cli.py         命令行入口（audit / batch / trend / coord / collab /
                 rulepack / ruleswitch / init-gate / gui）
  pipeline.py    流程编排
tools/
  make_sample_ifc.py  样例模型生成
```

## 已知限制

- 核查在二维水平面进行，适用于常规正交墙体；斜墙（挤出方向带水平分量）
  在参数化解析中按 RefDirection 处理，曲面墙的缺口检测精度有限。
- 无 GPU / 无显示的服务器上 PyVista 交互窗口与离屏渲染不可用，
  会自动改用 matplotlib 三维图；表格与平面标注图不受影响。
- 房间-门窗归属按平面距离关联，门位于两个房间边界时会计入两侧。
