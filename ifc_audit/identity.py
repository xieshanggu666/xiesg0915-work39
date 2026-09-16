"""单体稳定标识：跨批次一致、可持久区分的模型身份键。

背景
----
工单指纹 / 复查覆盖 / 回写都需要一个**稳定的单体标识**。两个极端都不可取：

* 只用文件名（``样例-建筑模型``）：不同目录下的同名模型（``A区/楼A.ifc``、
  ``B区/楼A.ifc``）会撞键，导致工单指纹共享、复查状态互相覆盖；
* 用批次内消歧显示名（``A区-楼A``）：前缀深度随**本批纳入的文件组成**变化，
  单独复查一个文件时前缀消失，身份随批次漂移。

本模块把两者分开：

* :func:`display_unit_name` —— 批次内**显示名**（:func:`unique_unit_names`），
  只用于报告 / 控制台，允许随批次组成变化；
* :func:`stable_unit_key` —— 工单身份用的**稳定标识**，等于模型相对
  **模型根锚点**的 POSIX 相对路径（去后缀），如 ``A区/楼A``、``B区/楼A``。

模型根锚点（model root）
------------------------
相对路径需要一个固定锚点，否则单独复查 ``A区/楼A.ifc`` 时又会退化成纯文件名。
锚点按以下优先级确定，并持久化到项目级配置 ``model_root.json``：

1. 显式参数 / CLI ``--model-root``；
2. 项目历史配置中已固化的锚点（一旦确定，后续批次沿用，保证跨批次一致）；
3. 本批全部文件的最长公共父目录（自动推断）。

只扫子目录时，锚点仍取历史固化值（优先）或公共父目录；同一工作区内
``A区/楼A.ifc`` 与 ``B区/楼A.ifc`` 因此始终得到不同的稳定键。
"""

from __future__ import annotations

import json
import os
import re
from typing import Optional


def _slug(name: str) -> str:
    return re.sub(r"[^\w一-鿿.-]+", "_", name).strip("_") or "project"


def to_posix_rel(path: str, root: str) -> str:
    """把绝对路径转成相对 ``root`` 的 POSIX 相对路径（保留目录层级）。"""
    rel = os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    return rel.replace(os.sep, "/")


def strip_ifc_suffix(rel_posix: str) -> str:
    """去掉 IFC 后缀（.ifc / .ifcxml / .ifczip，大小写不敏感）。"""
    low = rel_posix.lower()
    for suf in (".ifcxml", ".ifczip", ".ifc"):
        if low.endswith(suf):
            return rel_posix[: -len(suf)]
    return rel_posix


def common_parent_dir(files: list[str]) -> str:
    """一批文件的最长公共父目录（单文件时取其所在目录）。"""
    if not files:
        return os.getcwd()
    abs_files = [os.path.abspath(f) for f in files]
    if len(abs_files) == 1:
        return os.path.dirname(abs_files[0])
    # commonpath 要求同盘；失败时退化为公共字符串前缀所在目录
    try:
        common = os.path.commonpath(abs_files)
    except ValueError:
        common = os.path.dirname(os.path.commonprefix(abs_files))
    # commonpath 可能落在某个文件上（一批只有同目录文件时返回目录，正常；
    # 但保险起见，若 common 本身是文件则取其父目录）
    if os.path.isfile(common):
        common = os.path.dirname(common)
    return common


def infer_model_root(files: list[str], explicit: str = "") -> str:
    """推断模型根锚点：显式值优先，否则取本批文件公共父目录。"""
    if explicit:
        return os.path.abspath(explicit)
    return common_parent_dir(files)


def model_root_config_path(history_dir: str, project: str) -> str:
    """项目级模型根锚点配置路径：``<history>/<项目>/model_root.json``。"""
    return os.path.join(history_dir, _slug(project), "model_root.json")


def resolve_model_root(files: list[str], *, project: str = "",
                       history_dir: str = "", explicit: str = "",
                       persist: bool = True) -> tuple[str, bool]:
    """确定本批模型根锚点，并在首次确定时持久化到项目配置。

    Returns:
        (model_root, reused)。``reused`` 表示是否沿用了历史固化锚点。
    """
    cfg_path = ""
    saved_root = ""
    if history_dir and project:
        cfg_path = model_root_config_path(history_dir, project)
        if os.path.exists(cfg_path):
            try:
                with open(cfg_path, "r", encoding="utf-8") as f:
                    saved_root = json.load(f).get("model_root", "")
            except (OSError, json.JSONDecodeError):
                saved_root = ""

    if explicit:
        root = os.path.abspath(explicit)
    elif saved_root:
        return saved_root, True
    else:
        root = infer_model_root(files)

    if persist and cfg_path and root and not os.path.exists(cfg_path):
        _write_model_root(cfg_path, root, project, source="explicit" if explicit
                          else "auto")
    return root, False


def _write_model_root(cfg_path: str, root: str, project: str,
                      source: str = "auto") -> None:
    os.makedirs(os.path.dirname(os.path.abspath(cfg_path)), exist_ok=True)
    tmp = cfg_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"project": project, "model_root": root,
                   "source": source}, f, ensure_ascii=False, indent=2)
    os.replace(tmp, cfg_path)


def save_model_root(history_dir: str, project: str, root: str,
                    source: str = "explicit") -> str:
    """显式固化 / 更新项目模型根锚点。"""
    cfg_path = model_root_config_path(history_dir, project)
    _write_model_root(cfg_path, os.path.abspath(root), project, source)
    return cfg_path


def stable_unit_key(file_path: str, model_root: str = "") -> str:
    """单体跨批次稳定标识：相对模型根的 POSIX 路径去后缀。

    无模型根时退化为文件名去后缀（与旧版一致）；有根时保留目录层级，
    从而区分不同目录下的同名模型（``A区/楼A`` ≠ ``B区/楼A``）。
    """
    base = os.path.splitext(os.path.basename(file_path))[0]
    if not model_root:
        return base
    rel = to_posix_rel(file_path, model_root)
    key = strip_ifc_suffix(rel)
    # 文件不在根之下（relpath 出现 ``..``）时退回文件名，避免不稳定的上级引用
    if key.startswith(".."):
        return base
    return key or base


def build_unit_keys(files: list[str], model_root: str = ""
                    ) -> dict[str, str]:
    """为一批 ``{绝对文件路径: 稳定单体键}``。"""
    return {os.path.abspath(fp): stable_unit_key(fp, model_root) for fp in files}
