#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import re
from pathlib import Path
from typing import Any, List, Optional, Tuple


# ==========================
# 直接在这里配置（不使用命令行参数）
# ==========================
INPUT_ROOT = r"E:\Code\datapull\extract_data_origin\extract_data_2026-01-16_19-10-15"     # 输入根目录
OUTPUT_ROOT = r"E:\Code\datapull\temp"   # 输出根目录
FILE_GLOB = "**/*.json"                # 递归扫描所有 json


# ==========================
# index 配置（基于你给的标准格式）
# 0 材料名称
# 1 张力名称
# 2 张力值
# 3 张力单位
# 4 屈服名称
# 5 屈服值
# 6 屈服单位
# 7 伸长率名称
# 8 伸长率值
# 9 伸长率单位
# 10..(n-2) 元素字段（可能是 Mn元素含量：0.1 或 Mn：0.1）
# (n-1) ProcessDescription：...
# ==========================
TENSILE_IDX = 2
YIELD_IDX = 5
ELONG_IDX = 8
ELEMENT_START_IDX = 10


# 兼容中文/英文逗号（index 依赖“标准使用中文逗号”，英文逗号仅兜底）
SPLIT_RE = re.compile(r"，")
NUM_RE = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][-+]?\d+)?")


def _extract_number_from_part(part: str) -> Optional[float]:
    """
    从字段片段中提取数值：
    - 优先取冒号“：”右侧
    - 若无“：”，则尝试取第一个数字
    """
    s = part.strip()
    if "：" in s:
        right = s.split("：", 1)[1].strip()
        m = NUM_RE.search(right)
        return float(m.group(0)) if m else None
    m = NUM_RE.search(s)
    return float(m.group(0)) if m else None


def _split_preserving_process_description(record: str) -> List[str]:
    """
    ProcessDescription 里可能包含大量 '，'，直接 split 会破坏 index。
    做法：找到 'ProcessDescription：'，其后的内容整体作为最后一个字段保留。
    """
    s = record.strip()
    key = "ProcessDescription："
    pos = s.find(key)
    if pos == -1:
        # 没有 ProcessDescription 时，只能全量 split（数据本身不标准时会影响 index）
        return [p.strip() for p in SPLIT_RE.split(s) if p.strip()]

    head = s[:pos].strip()
    tail = s[pos:].strip()
    head_parts = [p.strip() for p in SPLIT_RE.split(head) if p.strip()]
    return head_parts + [tail]


def _record_passes_by_index(record: str) -> bool:
    """
    按 index 过滤：
    1) 张力值/屈服值/伸长率值：至少有一个非0（不能全为0）
    2) 元素字段：至少有一个非0（不能全为0）
    """
    parts = _split_preserving_process_description(record)

    # 必须至少包含：0..9 + 至少1个元素字段(10) + ProcessDescription(最后)
    if len(parts) < ELEMENT_START_IDX + 2:
        return False

    # 三个关键数值必须能提取出来
    t = _extract_number_from_part(parts[TENSILE_IDX]) if len(parts) > TENSILE_IDX else None
    y = _extract_number_from_part(parts[YIELD_IDX]) if len(parts) > YIELD_IDX else None
    e = _extract_number_from_part(parts[ELONG_IDX]) if len(parts) > ELONG_IDX else None
    if t is None or y is None or e is None:
        return False

    if not any(abs(v) > 0.0 for v in (t, y, e)):
        return False

    # 元素字段：从 ELEMENT_START_IDX 到 倒数第二项（最后一项是 ProcessDescription）
    element_parts = parts[ELEMENT_START_IDX:-1]
    if not element_parts:
        return False

    vals: List[float] = []
    for p in element_parts:
        v = _extract_number_from_part(p)
        if v is not None:
            vals.append(v)

    if not vals:
        return False

    if not any(abs(v) > 0.0 for v in vals):
        return False

    return True


def _validate_json_array(data: Any) -> Tuple[bool, str]:
    """
    判定文件是否满足“格式要求”：
    - 必须是 list
    - list 元素必须全部是 str（按你给的标准数据结构）
    """
    if not isinstance(data, list):
        return False, "root_not_array"
    for i, item in enumerate(data):
        if not isinstance(item, str):
            return False, f"item_not_string@index={i}"
    return True, "ok"


def _process_one_file(in_path: Path, out_path: Path) -> Tuple[str, int, int]:
    """
    返回：状态, 原始条数, 保留条数
    状态：
      - "saved"     已保存
      - "skip_bad"  格式不合格（不保存）
      - "skip_empty"过滤后为空（不保存）
    """
    abs_path = str(in_path.resolve())

    try:
        text = in_path.read_text(encoding="utf-8")
        data = json.loads(text)
    except Exception as ex:
        print(f"[SKIP_BAD] {abs_path}  reason=json_parse_failed  err={ex}")
        return "skip_bad", 0, 0

    ok, reason = _validate_json_array(data)
    if not ok:
        print(f"[SKIP_BAD] {abs_path}  reason={reason}")
        return "skip_bad", 0, 0

    kept = [item for item in data if _record_passes_by_index(item)]

    if len(kept) == 0:
        # 要求：过滤后为空则不保存，并打印绝对路径
        print(f"[SKIP_EMPTY] {abs_path}  total={len(data)} kept=0")
        return "skip_empty", len(data), 0

    # 需要保存
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(kept, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[SAVED] {abs_path}  ->  {str(out_path.resolve())}  total={len(data)} kept={len(kept)}")
    return "saved", len(data), len(kept)


def main():
    input_root = Path(INPUT_ROOT).resolve()
    output_root = Path(OUTPUT_ROOT).resolve()

    if not input_root.exists() or not input_root.is_dir():
        raise SystemExit(f"输入根目录不存在或不是目录：{input_root}")

    files = sorted(input_root.glob(FILE_GLOB))

    scanned = 0
    saved_files = 0
    skipped_bad = 0
    skipped_empty = 0
    total_items = 0
    total_kept = 0

    print(f"[START] input_root={input_root} output_root={output_root} files_found={len(files)}")

    for in_path in files:
        if not in_path.is_file():
            continue

        scanned += 1
        rel = in_path.relative_to(input_root)
        out_path = output_root / rel

        status, n, k = _process_one_file(in_path, out_path)
        total_items += n
        total_kept += k

        if status == "saved":
            saved_files += 1
        elif status == "skip_bad":
            skipped_bad += 1
        elif status == "skip_empty":
            skipped_empty += 1

    not_saved_files = skipped_bad + skipped_empty

    print(
        f"[DONE] scanned={scanned} saved_files={saved_files} "
        f"skipped_bad={skipped_bad} skipped_empty={skipped_empty} "
        f"not_saved_files={not_saved_files} "
        f"total_items={total_items} total_kept={total_kept}"
    )



if __name__ == "__main__":
    main()
