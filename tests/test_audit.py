"""端到端核查规则测试（不依赖 pytest，可直接运行）。

用法::

    python tests/test_audit.py
"""

from __future__ import annotations

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.make_sample_ifc import make_sample  # noqa: E402
from ifc_audit.pipeline import audit_ifc  # noqa: E402
from ifc_audit import report  # noqa: E402


def run() -> int:
    failures = []

    def check(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as td:
        ifc_path = os.path.join(td, "sample.ifc")
        make_sample(ifc_path)
        model = audit_ifc(ifc_path)

        s = model.summary()
        check(s["walls"] == 12, f"墙体 12 个（实际 {s['walls']}）")
        check(s["doors"] == 4, f"门 4 个（实际 {s['doors']}）")
        check(s["windows"] == 3, f"窗 3 个（实际 {s['windows']}）")
        check(s["rooms"] == 3, f"房间 3 个（实际 {s['rooms']}）")

        kinds = {}
        for i in model.issues:
            kinds[i.kind] = kinds.get(i.kind, 0) + 1

        check(kinds.get("duplicate_element") == 2,
              f"2 组重复构件（实际 {kinds.get('duplicate_element', 0)}）")
        check(kinds.get("wall_free_end") == 2,
              f"2 个自由墙端（实际 {kinds.get('wall_free_end', 0)}）")
        check(kinds.get("wall_end_gap") == 1,
              f"1 处墙段缺口（实际 {kinds.get('wall_end_gap', 0)}）")
        check(kinds.get("room_enclosure_gap") == 1,
              f"1 处房间围护缺口（实际 {kinds.get('room_enclosure_gap', 0)}）")
        check(kinds.get("area_mismatch") == 1,
              f"1 个面积偏差房间（实际 {kinds.get('area_mismatch', 0)}）")
        check(kinds.get("area_missing_declared") == 1,
              f"1 个无声明面积房间（实际 {kinds.get('area_missing_declared', 0)}）")
        check(kinds.get("opening_unassigned") == 2,
              f"2 个未归属房间的门窗（实际 {kinds.get('opening_unassigned', 0)}）")
        check(kinds.get("opening_size_anomaly") == 1,
              f"1 个尺寸异常门窗（实际 {kinds.get('opening_size_anomaly', 0)}）")

        # 房间 A/B 必须正确判定闭合（验证门覆盖门洞不误报）
        rooms = {r.name: r for r in model.rooms}
        check(rooms["A-101"].enclosed, "房间 A-101 闭合")
        check(rooms["B-102"].enclosed, "房间 B-102 闭合")
        check(not rooms["C-103"].enclosed, "房间 C-103 不闭合")
        check(abs(rooms["A-101"].net_area - 40.0) < 1e-6,
              "A-101 净面积 40 m²")
        check(abs(rooms["C-103"].net_area - 9.0) < 1e-6,
              "C-103 几何净面积 9 m²")
        check(rooms["A-101"].storey == "1F", "房间楼层解析为 1F")

        # ---- 门窗规格清单 ----
        items = {o.name.split("-")[0]: o for o in model.opening_items}
        check(set(items) == {"D1", "D1b", "D3", "D5", "WN1", "WN2", "WN3"},
              f"门窗明细覆盖全部 7 樘（实际 {sorted(items)}）")

        # IFC OverallWidth/Height 优先；缺失时包围盒推断（取水平长边为宽）
        check(abs(items["D3"].width - 0.9) < 1e-9
              and items["D3"].dim_source == "overall",
              "D3 宽高取 IFC OverallWidth/Height（0.9×2.1，来源 overall）")
        check(abs(items["WN1"].width - 1.2) < 1e-9
              and items["WN1"].dim_source == "bbox",
              "WN1 无 Overall 属性，包围盒推断宽 1.2m（来源 bbox）")
        check(abs(items["D1"].height - 2.1) < 1e-9
              and items["D1"].width >= 0.85,
              "D1 包围盒推断宽≈0.9m、高 2.1m（不取墙厚方向）")

        # 类型：ObjectType 优先，其次 PredefinedType，缺省为门/窗
        check(items["D1"].type_name == "实木门", "D1 类型取 ObjectType=实木门")
        check(items["D1b"].type_name == "门",
              "D1b 无 ObjectType 时类型缺省为“门”")

        # 归属：D5/WN2 游离；门位于两房间边界计入两侧
        check(items["D5"].unassigned and items["WN2"].unassigned,
              "游离门 D5 与游离窗 WN2 标记为未归属")
        check(items["D3"].room_names == ["A-101"], "外门 D3 仅归入 A-101")
        check(sorted(items["D1"].room_names) == ["A-101", "B-102"],
              "隔墙上的门 D1 计入两侧房间")

        # 尺寸异常：仅 WN3（300mm < 默认窗宽下限 400mm）
        check(items["WN3"].anomalous and not items["WN3"].unassigned,
              "WN3 尺寸异常但已归房间")
        check(not any(items[k].anomalous for k in ("D1", "D3", "WN1")),
              "正常门窗不误报尺寸异常")

        # 门窗表：按 楼层/房间/类别/类型/规格 归并计数
        sched = model.opening_schedule
        row_d1 = next(r for r in sched
                      if r.room_name == "A-101" and r.type_name == "实木门")
        check(row_d1.count == 1
              and abs(row_d1.width - 0.9) < 1e-9
              and abs(row_d1.height - 2.1) < 1e-9,
              "门窗表 A-101/实木门/900×2100 数量 1")
        unassigned_rows = [r for r in sched if r.room_global_id == ""]
        check({(r.kind, r.type_name) for r in unassigned_rows}
              == {("door", "超大门"), ("window", "固定窗")},
              "门窗表含“（未归属房间）”分组（D5、WN2）")
        check(all(r.n_anomalous == 0 for r in unassigned_rows),
              "游离的大尺寸门/窗不报尺寸异常")
        wn3_row = next(r for r in sched if r.type_name == "高窗")
        check(wn3_row.n_anomalous == 1 and "尺寸异常" in wn3_row.notes,
              "门窗表高窗行标注尺寸异常")

        # 房间清单中门窗计数同步更新
        check(rooms["B-102"].windows == 2, "B-102 含 WN1、WN3 两樘窗")

        # 墙段缺口长度应接近 0.15m
        gap = next(i for i in model.issues if i.kind == "wall_end_gap")
        check(abs(gap.measure - 0.15) < 0.02,
              f"墙段缺口长度 ≈0.15m（实际 {gap.measure:.3f}）")
        # 每条问题都带可定位的 GlobalId
        check(all(i.global_ids for i in model.issues),
              "所有问题均关联 GlobalId")

        # 导出
        xlsx = report.export_excel(model, os.path.join(td, "r.xlsx"))
        csv1 = report.export_issues_csv(model, os.path.join(td, "i.csv"))
        csv2 = report.export_rooms_csv(model, os.path.join(td, "r.csv"))
        png = report.export_annotated_plan(model, os.path.join(td, "p.png"))
        check(all(os.path.getsize(p) > 100 for p in (xlsx, csv1, csv2, png)),
              "Excel/CSV/PNG 导出成功且非空")

    # ---- 无几何房间：围护状态必须是“未检查”，不得给出闭合结论 ----
    import ifcopenshell
    p2 = os.path.join(td, "nogeo.ifc")
    make_sample(p2)
    f = ifcopenshell.open(p2)
    storey = f.by_type("IfcBuildingStorey")[0]
    nogeo = f.create_entity("IfcSpace", GlobalId=ifcopenshell.guid.new(),
                            Name="无几何房间")
    f.create_entity("IfcRelAggregates", GlobalId=ifcopenshell.guid.new(),
                    RelatingObject=storey, RelatedObjects=(nogeo,))
    f.write(p2)
    m2 = audit_ifc(p2)
    ng = next((r for r in m2.rooms if r.name == "无几何房间"), None)
    check(ng is not None, "无几何房间进入净面积清单")
    if ng is not None:
        check(ng.enclosure_status == "unchecked",
              f"无几何房间围护状态=未检查（实际 {ng.enclosure_status}）")
        check(ng.enclosure_label == "未检查",
              f"无几何房间围护显示=未检查（实际 {ng.enclosure_label}）")
        check(ng.net_area == 0.0, "无几何房间净面积为 0")
    check(any(i.kind == "room_no_geometry" for i in m2.issues),
          "无几何房间生成 room_no_geometry 警告")
    # 有几何房间不能被波及
    check(all(r.enclosure_status in ("closed", "open")
              for r in m2.rooms if r.name != "无几何房间"),
          "有几何房间围护状态仍为闭合/不闭合")

    print()
    if failures:
        print(f"{len(failures)} 项失败")
        return 1
    print("全部测试通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
