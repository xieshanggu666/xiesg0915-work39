"""单体稳定标识（identity）测试：跨批次一致、跨目录同名可区分。

用法::

    python tests/test_identity.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ifc_audit.identity import (  # noqa: E402
    stable_unit_key, build_unit_keys, common_parent_dir,
    resolve_model_root, model_root_config_path, strip_ifc_suffix,
)


def run() -> int:
    failures = []

    def check(cond, msg):
        print(("PASS " if cond else "FAIL ") + msg)
        if not cond:
            failures.append(msg)

    with tempfile.TemporaryDirectory() as d:
        proj = os.path.join(d, "proj")
        fa = os.path.join(proj, "A区", "楼A.ifc")
        fb = os.path.join(proj, "B区", "楼A.ifc")
        fc = os.path.join(proj, "A区", "机电.ifcxml")
        for f in (fa, fb, fc):
            os.makedirs(os.path.dirname(f), exist_ok=True)
            with open(f, "wb") as fh:
                fh.write(b"x")

        # 1) 不同目录同名文件键不同（核心回归）
        root = common_parent_dir([fa, fb])
        ka, kb = stable_unit_key(fa, root), stable_unit_key(fb, root)
        check(ka == "A区/楼A", f"A区键为相对路径（实际 {ka}）")
        check(kb == "B区/楼A", f"B区键为相对路径（实际 {kb}）")
        check(ka != kb, "不同目录同名模型稳定键不同（不共享工单指纹）")

        # 2) 无锚点退回文件名（兼容旧行为）
        check(stable_unit_key(fa, "") == "楼A", "无模型根时退回文件名")

        # 3) IFC 后缀剥离（含 .ifcxml / .ifczip，大小写不敏感）
        check(strip_ifc_suffix("A区/机电.ifcxml") == "A区/机电",
              ".ifcxml 后缀剥离")
        check(strip_ifc_suffix("x.IFC") == "x", "大写 .IFC 后缀剥离")

        # 4) 模型根持久化：首批确定后局部复查沿用，键不漂移
        hist = os.path.join(d, "history")
        r1, reused1 = resolve_model_root(
            [fa, fb], project="P", history_dir=hist)
        check(os.path.normcase(r1) == os.path.normcase(proj)
              and not reused1, "首批自动推断模型根为公共父目录")
        cfg = model_root_config_path(hist, "P")
        check(os.path.exists(cfg), "模型根固化到项目配置 model_root.json")
        # 只扫 A 区一个文件：仍沿用固化根，键保留 A区/ 前缀
        r2, reused2 = resolve_model_root(
            [fa], project="P", history_dir=hist)
        check(reused2 and os.path.normcase(r2) == os.path.normcase(proj),
              "局部复查沿用历史固化模型根（不退化为父目录）")
        check(stable_unit_key(fa, r2) == "A区/楼A",
              "局部复查稳定键不漂移（仍含目录层级）")

        # 5) 显式模型根优先
        custom = os.path.join(d, "custom_root")
        os.makedirs(custom, exist_ok=True)
        r3, reused3 = resolve_model_root(
            [fa], project="P", history_dir=hist, explicit=custom)
        check(os.path.normcase(r3) == os.path.normcase(custom),
              "显式 --model-root 优先于固化配置")

        # 6) build_unit_keys 以绝对路径为键
        keys = build_unit_keys([fa, fb], proj)
        check(keys.get(os.path.abspath(fa)) == "A区/楼A"
              and keys.get(os.path.abspath(fb)) == "B区/楼A",
              "build_unit_keys 返回 {绝对路径: 稳定键}")

        # 7) 单文件公共父目录是其所在目录
        check(common_parent_dir([fa]) == os.path.dirname(fa),
              "单文件公共父目录取其所在目录")

        # 8) 固化配置内容正确
        doc = json.load(open(cfg, encoding="utf-8"))
        check(doc["project"] == "P" and doc["model_root"],
              "model_root.json 记录项目与锚点目录")

    print()
    if failures:
        print(f"{len(failures)} 项测试失败：")
        for m in failures:
            print("  - " + m)
        return 1
    print("全部测试通过。")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
