# -*- coding: utf-8 -*-
"""
在你“最新版本代码”的基础上修改：
- 表格：只解析结构（caption/header/rows + cell id），不硬解析材料/成分/属性
- items：结构化表格 + property corpus 交给大模型抽取
- properties/composition：缺失严重才触发二次补全（fallback）
- 工艺路线：按材料分别抽取并写回（每篇一次 ProcessMap）
  【本次修复点】工艺链路必须“以该条目 Material 为终点”，而不是以 Material 为起点：
    1) prompt 强约束：最后一句必须明确“最终得到<Material>”
    2) 写回后处理：若未显式收束到 Material，则自动补尾“最终得到{Material}。”
输出保持不变：每篇输出一个 json 文件，内容是 target string 数组（item_to_target_string）。
"""

import json
import os
import re
import threading
from datetime import datetime
from typing import Any, Dict, List, Tuple

from openai import OpenAI
from tqdm import tqdm
from concurrent.futures import ThreadPoolExecutor, as_completed


# =============================
# 字段定义（保持你原来的输出结构）
# =============================

ALL_FIELDS = [
    "Material",
    "Tensile_name", "Tensile_value", "Tensile_unit",
    "Yield_name", "Yield_value", "Yield_unit",
    "Elongation_name", "Elongation_value", "Elongation_unit",
    "H","B","C","N","O","F","Na","Mg","Al","Si","P","S","Cl","Ca","Ti","V","Cr","Mn","Fe","Co","Ni","Cu","Zn",
    "As","Y","Zr","Nb","Mo","Sn","Sb","La","Ce","Ta","W","Pb","Bi",
    "ProcessDescription"
]

ELEMENT_FIELDS = [
    "H","B","C","N","O","F","Na","Mg","Al","Si","P","S","Cl","Ca","Ti","V","Cr","Mn","Fe","Co","Ni","Cu","Zn",
    "As","Y","Zr","Nb","Mo","Sn","Sb","La","Ce","Ta","W","Pb","Bi"
]

PROPERTY_KEYWORDS = [
    "mpa", "gpa", "yield", "ys", "0.2", "tensile", "uts", "ultimate", "strength",
    "elongation", "ductility", "strain", "fracture",
    "hardness", "hv", "hb", "hrc",
    "composition", "chemical", "wt%", "wt.%", "at%", "at.%", "balance", "bal."
]

PROCESS_KEYWORDS = [
    "experimental", "procedure", "materials", "methods",
    "heat", "treatment", "solution", "anneal", "aging", "quench",
    "rolling", "cold", "hot", "forging", "casting", "sinter",
    "specimen", "tensile", "hardness"
]


# =============================
# 基础工具
# =============================

def safe_float(x: Any) -> float:
    if x is None:
        return 0.0
    if isinstance(x, (int, float)):
        return float(x)
    s = str(x).strip()
    if s in ["", "–", "-", "—", "NA", "N/A", "n/a", "null", "None"]:
        return 0.0
    s = s.replace("×", "x")
    s2 = re.sub(r"[^\d\.\-eE+]", "", s)
    try:
        return float(s2)
    except Exception:
        return 0.0


def normalize_material_name(name: str) -> str:
    return re.sub(r"\s+", " ", str(name).strip())


def compact_space(s: str) -> str:
    return re.sub(r"\s+", " ", str(s or "")).strip()


def normalize_items_schema(item: Dict[str, Any]) -> Dict[str, Any]:
    """
    schema：元素缺失=0，其他缺失=""
    同时统一默认 name/unit，并把数值尽量转 float（失败不强制）。
    """
    out: Dict[str, Any] = {}
    for f in ALL_FIELDS:
        if f in item and item[f] is not None:
            out[f] = item[f]
        else:
            out[f] = 0 if f in ELEMENT_FIELDS else ""

    # 默认名称/单位
    if not out["Tensile_name"]:
        out["Tensile_name"] = "TS (MPa)"
    if not out["Tensile_unit"]:
        out["Tensile_unit"] = "MPa"
    if not out["Yield_name"]:
        out["Yield_name"] = "YS (MPa)"
    if not out["Yield_unit"]:
        out["Yield_unit"] = "MPa"
    if not out["Elongation_name"]:
        out["Elongation_name"] = "f-EL (%)"
    if not out["Elongation_unit"]:
        out["Elongation_unit"] = "%"

    # 数值字段尽量转 float
    for k in ["Tensile_value", "Yield_value", "Elongation_value"] + ELEMENT_FIELDS:
        v = out.get(k)
        if isinstance(v, str):
            vv = v.strip()
            if vv == "":
                out[k] = 0.0
            else:
                try:
                    out[k] = float(vv)
                except Exception:
                    out[k] = safe_float(v)
        elif v is None:
            out[k] = 0.0

    # 关键字段清洗
    out["Material"] = normalize_material_name(out.get("Material", ""))
    out["ProcessDescription"] = compact_space(out.get("ProcessDescription", ""))

    return out


def ensure_process_ends_with_material(desc: str, material: str) -> str:
    """
    保证工艺链路“终点=material”：
    - 若 desc 为空：返回空
    - 若 desc 中已经明确包含“最终得到/得到/获得 ... material”之类：不强行重复
    - 否则在末尾补一句“最终得到{material}。”
    """
    desc = compact_space(desc)
    material = normalize_material_name(material)

    if not desc or not material:
        return desc

    # 已经明确收束到 material（宽松判断）
    tail = desc[-120:] if len(desc) > 120 else desc
    if material in tail and re.search(r"(最终得到|最终获得|得到|获得|制得|形成|生成)", tail):
        return desc

    # 末尾已经出现 material 但没有动词，也视为已收束
    if material in tail:
        return desc

    # 补尾，确保终点
    if not desc.endswith(("。", "；", "！", "？")):
        desc += "。"
    desc += f"最终得到{material}。"
    return compact_space(desc)


# =============================
# 表格结构化（只解析结构，不硬解析语义）
# =============================

def guess_header_from_rows(table: Dict[str, Any]) -> Tuple[List[str], List[List[Any]]]:
    header = table.get("header") or []
    rows = table.get("rows") or []
    if isinstance(header, list) and header:
        return [str(x) for x in header], rows if isinstance(rows, list) else []

    # 尝试把第一行当 header
    if isinstance(rows, list) and len(rows) >= 2 and isinstance(rows[0], list):
        row0 = rows[0]
        row1 = rows[1] if isinstance(rows[1], list) else []
        row0_text = " ".join([str(x) for x in row0])
        row1_text = " ".join([str(x) for x in row1])
        if re.search(r"[A-Za-z%°\(\)\-/]", row0_text) and not re.search(r"[A-Za-z]", row1_text):
            return [str(x) for x in row0], rows[1:]
    return [], rows if isinstance(rows, list) else []


def table_to_markdown_with_cell_ids(table: Dict[str, Any], max_rows: int = 40, max_cols: int = 50) -> str:
    """
    输出 Markdown 风格表格，并给每个单元格加 cell id：
    - 表头：h1c1, h1c2 ...
    - 数据：r1c1, r1c2 ...
    同时包含 label + caption。
    """
    label = compact_space(table.get("label", ""))
    caption = compact_space(table.get("caption", ""))
    header, rows = guess_header_from_rows(table)

    def trim_row(r: List[Any]) -> List[str]:
        rr = ["" if x is None else compact_space(x) for x in r]
        if len(rr) > max_cols:
            rr = rr[:max_cols] + ["..."]
        return rr

    header = trim_row(header) if header else []
    rows = [trim_row(r) for r in rows[:max_rows] if isinstance(r, list)]

    lines: List[str] = []
    title = (label + ": " + caption).strip(": ").strip()
    if title:
        lines.append(title)

    if header:
        header_cells = [f"{v} [h1c{i}]" for i, v in enumerate(header, start=1)]
        lines.append("| " + " | ".join(header_cells) + " |")
        lines.append("| " + " | ".join(["---"] * len(header_cells)) + " |")
        for r_i, row in enumerate(rows, start=1):
            cells = [f"{v} [r{r_i}c{c_i}]" for c_i, v in enumerate(row, start=1)]
            while len(cells) < len(header_cells):
                cells.append(f" [r{r_i}c{len(cells)+1}]")
            lines.append("| " + " | ".join(cells) + " |")
    else:
        for r_i, row in enumerate(rows, start=1):
            cells = [f"{v} [r{r_i}c{c_i}]" for c_i, v in enumerate(row, start=1)]
            lines.append("| " + " | ".join(cells) + " |")

    return "\n".join(lines).strip()


def build_tables_payload(tables: List[Dict[str, Any]], max_chars: int = 20000) -> str:
    chunks: List[str] = []
    for t in tables or []:
        chunks.append(table_to_markdown_with_cell_ids(t))
        chunks.append("")
    text = "\n".join(chunks).strip()
    if len(text) > max_chars:
        head = text[: max_chars // 2]
        tail = text[-max_chars // 2:]
        text = head + "\n...\n" + tail
    return text


# =============================
# sections_tree / predata：构建证据语料
# =============================

def walk_sections_collect(nodes: List[Dict[str, Any]], keywords: List[str]) -> List[str]:
    out = []
    kw = [k.lower() for k in keywords]
    for n in nodes or []:
        title = (n.get("title") or "")
        text = (n.get("text") or "")
        blob = (title + "\n" + text).lower()
        if any(k in blob for k in kw) and text.strip():
            out.append(f"{title}\n{text}".strip())
        subs = n.get("subsections") or []
        out.extend(walk_sections_collect(subs, keywords))
    return out


def build_property_corpus(data: Dict[str, Any], max_chars: int = 18000) -> str:
    st = data.get("sections_tree", [])
    if isinstance(st, list) and st:
        parts = walk_sections_collect(st, PROPERTY_KEYWORDS)
        uniq, seen = [], set()
        for p in parts:
            p2 = p.strip()
            if not p2:
                continue
            key = p2[:200]
            if key not in seen:
                uniq.append(p2)
                seen.add(key)
        corpus = "\n\n".join(uniq)
        corpus = re.sub(r"\n{3,}", "\n\n", corpus).strip()
        return corpus[:max_chars]

    predata = data.get("predata", [])
    if isinstance(predata, list):
        txt = "\n\n".join([str(x) for x in predata if str(x).strip()])
    else:
        txt = str(predata)

    paras = re.split(r"\n\s*\n", txt)
    keep = []
    for p in paras:
        pl = p.lower()
        if any(k in pl for k in PROPERTY_KEYWORDS):
            keep.append(p.strip())
    corpus = "\n\n".join(keep).strip()
    return corpus[:max_chars] if corpus else txt[:max_chars]


def build_process_corpus(data: Dict[str, Any], max_chars: int = 16000) -> str:
    st = data.get("sections_tree", [])
    if isinstance(st, list) and st:
        parts = walk_sections_collect(st, PROCESS_KEYWORDS)
        uniq, seen = [], set()
        for p in parts:
            p2 = p.strip()
            if not p2:
                continue
            key = p2[:200]
            if key not in seen:
                uniq.append(p2)
                seen.add(key)
        corpus = "\n\n".join(uniq)
        corpus = re.sub(r"\n{3,}", "\n\n", corpus).strip()
        return corpus[:max_chars]

    predata = data.get("predata", [])
    if isinstance(predata, list):
        corpus = "\n\n".join([str(x) for x in predata if str(x).strip()])
    else:
        corpus = str(predata)
    corpus = re.sub(r"\n{3,}", "\n\n", corpus).strip()
    return corpus[:max_chars]


# =============================
# LLM 工具（线程内 client）
# =============================

_thread_local = threading.local()

def get_thread_client(base_url: str, api_key: str) -> OpenAI:
    if getattr(_thread_local, "client", None) is None:
        _thread_local.client = OpenAI(base_url=base_url, api_key=api_key)
    return _thread_local.client


def _strip_code_fences(s: str) -> str:
    s = (s or "").strip()
    s = re.sub(r"</?think>", "", s, flags=re.IGNORECASE).strip()
    s = re.sub(r"^```json\s*", "", s, flags=re.IGNORECASE)
    s = re.sub(r"^```\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()


def call_llm_json(
    client: OpenAI,
    prompt: str,
    model: str,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    retries: int = 3
) -> Dict[str, Any]:
    last_err = None
    cur_prompt = prompt
    for _ in range(retries):
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": cur_prompt}],
            temperature=temperature,
            max_tokens=max_tokens
        )
        content = _strip_code_fences(resp.choices[0].message.content or "")
        try:
            return json.loads(content)
        except Exception as e:
            last_err = str(e)

        try:
            i = content.find("{")
            j = content.rfind("}")
            if i != -1 and j != -1 and j > i:
                return json.loads(content[i:j+1])
        except Exception as e:
            last_err = str(e)

        cur_prompt = (
            prompt
            + "\n\n【你刚才输出的不是合法 JSON。请只输出纯 JSON，不要解释，不要 Markdown。】"
            + f"\n【解析错误：{last_err}】"
        )
    raise RuntimeError(f"LLM 输出无法解析为 JSON，最后错误：{last_err}")


# =============================
# LLM：从“结构化表格 + 证据正文”抽 items
# =============================

def build_extract_items_prompt(property_corpus: str, tables_md: str) -> str:
    elems_obj = ", ".join([f"\"{e}\": 0" for e in ELEMENT_FIELDS])
    return f"""
你是材料科学信息抽取专家。下面给你两部分证据：
1) 性能/成分相关正文（已筛选）
2) 全部表格（仅结构化展示，保留 caption/header/rows，并为单元格加了 cell id）

你的任务：抽取材料条目 items。每条 item 必须包含：
- Material（尽量沿用论文写法，并包含状态/热处理条件以区分样品）
- Tensile_value（TS/UTS，单位默认 MPa；若单位是 GPa 请换算成 MPa）
- Yield_value（YS/0.2%YS，单位默认 MPa；若单位是 GPa 请换算成 MPa）
- Elongation_value（延伸率，单位默认 %；若给的是小数应变请换算成 %）
- 元素含量（仅当证据明确出现时填写；否则 0；Balance/Bal. 视为 0，不要猜补）

【严格输出：只输出纯 JSON，不要解释，不要 Markdown】
格式：
{{
  "items": [
    {{
      "Material": "string",
      "Tensile_value": 0,
      "Yield_value": 0,
      "Elongation_value": 0,
      {elems_obj}
    }}
  ]
}}

【规则（非常重要）】
1) 只写证据中明确出现的数值；找不到必须填 0。
2) 如果同一个基础合金有不同状态（如 solution-treated / aged 500°C 30h / cold-rolled 50%），必须拆成不同 Material。
3) 如果表格给了多行材料，但只给了一部分性能，也要把材料列出来，缺失字段填 0。
4) 允许你综合正文+表格来确定材料名/状态，但不要臆测不存在的材料。
5) 不要输出任何额外字段（不要输出单位字段、不要输出 ProcessDescription）。

【证据正文（性能/成分相关段落）】
{property_corpus}

【表格（结构 + cell id）】
{tables_md}
""".strip()


def extract_items_with_llm(
    base_url: str,
    api_key: str,
    model: str,
    data: Dict[str, Any],
    max_tokens: int = 4096
) -> List[Dict[str, Any]]:
    property_corpus = build_property_corpus(data)
    tables_md = build_tables_payload(data.get("tables", []) or [])

    client = get_thread_client(base_url, api_key)
    prompt = build_extract_items_prompt(property_corpus, tables_md)
    result = call_llm_json(client, prompt, model=model, max_tokens=max_tokens, temperature=0.0, retries=3)

    out_items = result.get("items", [])
    if not isinstance(out_items, list):
        return []

    items: List[Dict[str, Any]] = []
    for it in out_items:
        if not isinstance(it, dict):
            continue
        mat = normalize_material_name(it.get("Material", ""))
        if not mat:
            continue

        new_it = {f: (0 if f in ELEMENT_FIELDS else "") for f in ALL_FIELDS}
        new_it["Material"] = mat

        new_it["Tensile_name"] = "TS (MPa)"
        new_it["Tensile_value"] = safe_float(it.get("Tensile_value", 0))
        new_it["Tensile_unit"] = "MPa"

        new_it["Yield_name"] = "YS (MPa)"
        new_it["Yield_value"] = safe_float(it.get("Yield_value", 0))
        new_it["Yield_unit"] = "MPa"

        new_it["Elongation_name"] = "f-EL (%)"
        new_it["Elongation_value"] = safe_float(it.get("Elongation_value", 0))
        new_it["Elongation_unit"] = "%"

        for e in ELEMENT_FIELDS:
            new_it[e] = safe_float(it.get(e, 0))

        new_it["ProcessDescription"] = ""
        items.append(new_it)

    uniq: Dict[str, Dict[str, Any]] = {}
    for it in items:
        key = f"{it['Material']}||{it['Tensile_value']}||{it['Yield_value']}||{it['Elongation_value']}"
        uniq[key] = it
    return list(uniq.values())


# =============================
# 工艺抽取：按材料分别抽取并写回（修复：终点=Material）
# =============================

def build_material_process_prompt(
    materials: List[str],
    process_corpus: str,
    tables_md: str
) -> str:
    """
    一次性让模型按 Material 输出 ProcessDescription（终点=Material）。
    """
    mats = [normalize_material_name(m) for m in materials if normalize_material_name(m)]
    mats = list(dict.fromkeys(mats))  # keep order unique

    return f"""
你是材料加工工艺抽取专家。下面给你一篇论文的工艺相关证据（正文 + 表格结构），以及该论文中已识别出的材料列表。

你的任务：对每一个 Material，抽取“以该 Material 为终点”的工艺链路/热处理/变形/制备步骤，并写成一段可独立阅读的中文描述。

【必须输出纯 JSON，不要解释，不要 Markdown】
格式：
{{
  "materials": [
    {{
      "Material": "必须与输入完全一致",
      "ProcessDescription": "中文工艺链路（可独立阅读，必须消除指代，不能出现‘该材料/该样品/如图/如表/上述’等；尽可能包含关键参数：温度、时间、压下率/应变/道次、冷却方式、气氛等；必须体现链路方向：从上游原料/前序状态 -> 若干工艺步骤 -> 最终得到本条 Material）"
    }}
  ]
}}

【关键规则（非常重要，必须遵守）】
1) 必须对输入 materials 列表中的每个 Material 都输出一条记录（不能遗漏）。
2) 只写证据中明确出现的工艺，不要臆测；如果证据不足以区分该 Material 的专属链路，ProcessDescription 允许输出空字符串 ""。
3) 【链路方向强约束】ProcessDescription 必须以“得到本条 Material”收束：最后一句必须明确写出“最终得到<Material>”（其中 <Material> 用该条 Material 的原文字符串）。
4) 不要输出数组以外的字段，不要夹带任何额外文本。

【输入 materials 列表（请逐条覆盖）】
{json.dumps(mats, ensure_ascii=False)}

【工艺相关正文（已筛选）】
{process_corpus}

【表格（结构 + cell id；可能包含热处理/状态信息）】
{tables_md}
""".strip()


def get_material_process_map(
    base_url: str,
    api_key: str,
    model: str,
    materials: List[str],
    process_corpus: str,
    tables_md: str
) -> Dict[str, str]:
    if not materials:
        return {}

    client = get_thread_client(base_url, api_key)
    prompt = build_material_process_prompt(materials, process_corpus, tables_md)
    result = call_llm_json(client, prompt, model=model, max_tokens=4096, temperature=0.0, retries=3)

    out = {}
    arr = result.get("materials", [])
    if not isinstance(arr, list):
        return out

    for rec in arr:
        if not isinstance(rec, dict):
            continue
        m = normalize_material_name(rec.get("Material", ""))
        d = compact_space(rec.get("ProcessDescription", ""))
        if m:
            out[m] = d
    return out


# =============================
# 兜底：CommonProcess（用于材料级抽取缺失时的 fallback）
# =============================

def build_common_process_prompt(process_corpus: str) -> str:
    return f"""
你是材料加工工艺抽取专家。请你只根据我提供的“工艺相关正文”，抽取并重写为可独立阅读的完整工艺链路。

【必须输出的 JSON 格式（只输出纯 JSON，不要任何解释）】
{{
  "CommonProcess": "中文工艺链路描述（必须消除指代，不能出现‘样品/该材料/如图/如表’等；必须包含关键参数：温度、时间、压下率/应变/道次、冷却方式、气氛等；描述逻辑为：上游原料/前序状态 + 工艺 -> ... -> 得到目标状态材料）"
}}

【要求】
1) 只写真实出现的工艺，不要臆测。
2) 若文本中出现多个阶段（铸造→轧制→固溶→淬火→时效等），必须按顺序写全。
3) 不要输出数组，不要输出多条记录，只输出一个 JSON 对象。

【工艺相关正文】
{process_corpus}
""".strip()


def get_common_process_description(base_url: str, api_key: str, model: str, process_corpus: str) -> str:
    client = get_thread_client(base_url, api_key)
    prompt = build_common_process_prompt(process_corpus)
    result = call_llm_json(client, prompt, model=model, max_tokens=2048, temperature=0.0, retries=3)
    cp = str(result.get("CommonProcess", "")).strip()
    return cp if cp else "未在工艺相关正文中抽取到明确工艺链路。"


# =============================
# 轻量补全：仅在缺失严重时触发
# =============================

def need_fill_properties(items: List[Dict[str, Any]], zero_ratio_threshold: float = 0.5) -> bool:
    if not items:
        return False
    zero_cnt = 0
    for it in items:
        if float(it.get("Tensile_value", 0) or 0) == 0 and float(it.get("Yield_value", 0) or 0) == 0:
            zero_cnt += 1
    return (zero_cnt / max(1, len(items))) >= zero_ratio_threshold


def build_fill_properties_prompt(items: List[Dict[str, Any]], property_corpus: str, tables_md: str) -> str:
    items_brief = []
    for it in items:
        items_brief.append({
            "Material": it.get("Material", ""),
            "Tensile_value": it.get("Tensile_value", 0),
            "Yield_value": it.get("Yield_value", 0),
            "Elongation_value": it.get("Elongation_value", 0),
            "CompositionNonZero": {e: it.get(e, 0) for e in ELEMENT_FIELDS if float(it.get(e, 0) or 0) != 0}
        })

    elems_obj = ", ".join([f"\"{e}\": 0" for e in ELEMENT_FIELDS])

    return f"""
你是材料科学信息抽取专家。我已经抽到了材料条目，但性能/成分缺失很多（为0）。
请结合“证据正文（只包含性能/成分相关段落）”与“表格（结构+cell id）”，为每个材料补全缺失字段。

【规则】
1) 只能在证据中明确出现时填写；找不到必须填0。
2) 只补缺失字段：若输入当前值不是0，必须保持不变。
3) 输出必须是纯 JSON（不要解释，不要 Markdown），格式：
{{
  "items":[
    {{
      "Material":"必须与输入完全一致",
      "Tensile_value": 0,
      "Yield_value": 0,
      "Elongation_value": 0,
      {elems_obj}
    }}
  ]
}}

【输入：当前条目（0代表缺失）】
{json.dumps(items_brief, ensure_ascii=False)}

【证据正文】
{property_corpus}

【表格（结构 + cell id）】
{tables_md}
""".strip()


def fill_missing_properties_with_llm(
    base_url: str,
    api_key: str,
    model: str,
    items: List[Dict[str, Any]],
    data: Dict[str, Any]
) -> List[Dict[str, Any]]:
    property_corpus = build_property_corpus(data)
    tables_md = build_tables_payload(data.get("tables", []) or [])
    prompt = build_fill_properties_prompt(items, property_corpus, tables_md)

    client = get_thread_client(base_url, api_key)
    result = call_llm_json(client, prompt, model=model, max_tokens=4096, temperature=0.0, retries=3)
    out_items = result.get("items", [])
    if not isinstance(out_items, list):
        return items

    by_mat = {str(it.get("Material", "")).strip(): it for it in out_items if isinstance(it, dict)}
    for it in items:
        mat = str(it.get("Material", "")).strip()
        src = by_mat.get(mat, {})

        for k in ["Tensile_value", "Yield_value", "Elongation_value"]:
            if float(it.get(k, 0) or 0) == 0 and k in src:
                v = safe_float(src.get(k))
                if v != 0:
                    it[k] = v

        for e in ELEMENT_FIELDS:
            if float(it.get(e, 0) or 0) == 0 and e in src:
                v = safe_float(src.get(e))
                if v != 0:
                    it[e] = v

    return items


# =============================
# 强 fallback：当 items 为空时触发
# =============================

def build_strong_fallback_prompt(property_corpus: str, tables_md: str) -> str:
    elems_obj = ", ".join([f"\"{e}\": 0" for e in ELEMENT_FIELDS])
    return f"""
你是材料科学信息抽取专家。当前“未能从证据中抽取到任何材料条目”，请你更激进地从证据中抽取所有可能的材料条目（仍然禁止臆测数值）。

【必须输出纯 JSON（不要解释，不要 Markdown），格式如下】
{{
  "items":[
    {{
      "Material":"材料名称（尽可能包含状态/处理条件以区分不同样品）",
      "Tensile_value": 0,
      "Yield_value": 0,
      "Elongation_value": 0,
      {elems_obj}
    }}
  ]
}}

【规则】
1) 只写证据中明确出现的数值；无法找到或无法对应材料则填0。
2) 如果证据中出现多个材料（含中间状态样品），尽量都列出。
3) Material 必须与证据文本中的命名一致，避免你自己发明缩写。
4) TS/YS/EL 对应：抗拉强度/UTS、屈服强度/YS、延伸率/Elongation。
5) 元素含量只在证据明确时填写（wt%/at%）；否则为0。

【证据正文（性能/成分相关段落）】
{property_corpus}

【表格（结构 + cell id）】
{tables_md}
""".strip()


def strong_fallback_extract_items_with_llm(
    base_url: str,
    api_key: str,
    model: str,
    data: Dict[str, Any]
) -> List[Dict[str, Any]]:
    property_corpus = build_property_corpus(data)
    tables_md = build_tables_payload(data.get("tables", []) or [], max_chars=22000)
    prompt = build_strong_fallback_prompt(property_corpus, tables_md)

    client = get_thread_client(base_url, api_key)
    result = call_llm_json(client, prompt, model=model, max_tokens=4096, temperature=0.0, retries=3)
    out_items = result.get("items", [])
    if not isinstance(out_items, list):
        return []

    items: List[Dict[str, Any]] = []
    for it in out_items:
        if not isinstance(it, dict):
            continue
        mat = str(it.get("Material", "")).strip()
        if not mat:
            continue

        new_it = {f: (0 if f in ELEMENT_FIELDS else "") for f in ALL_FIELDS}
        new_it["Material"] = normalize_material_name(mat)

        new_it["Tensile_name"] = "TS (MPa)"
        new_it["Tensile_value"] = safe_float(it.get("Tensile_value"))
        new_it["Tensile_unit"] = "MPa"

        new_it["Yield_name"] = "YS (MPa)"
        new_it["Yield_value"] = safe_float(it.get("Yield_value"))
        new_it["Yield_unit"] = "MPa"

        new_it["Elongation_name"] = "f-EL (%)"
        new_it["Elongation_value"] = safe_float(it.get("Elongation_value"))
        new_it["Elongation_unit"] = "%"

        for e in ELEMENT_FIELDS:
            new_it[e] = safe_float(it.get(e, 0))

        new_it["ProcessDescription"] = ""
        items.append(new_it)

    uniq = {}
    for it in items:
        key = f"{it['Material']}||{it['Tensile_value']}||{it['Yield_value']}||{it['Elongation_value']}"
        uniq[key] = it
    return list(uniq.values())


# =============================
# 输出字符串（保持不变）
# =============================

def item_to_target_string(item: Dict[str, Any]) -> str:
    item = normalize_items_schema(item)
    segs = [
        f"材料名称：{item['Material']}",
        f"张力名称：{item['Tensile_name']}",
        f"张力值：{item['Tensile_value']}",
        f"张力单位：{item['Tensile_unit']}",
        f"屈服名称：{item['Yield_name']}",
        f"屈服值：{item['Yield_value']}",
        f"屈服单位：{item['Yield_unit']}",
        f"伸长率名称：{item['Elongation_name']}",
        f"伸长率值：{item['Elongation_value']}",
        f"伸长率单位：{item['Elongation_unit']}",
    ]
    for e in ELEMENT_FIELDS:
        segs.append(f"{e}：{item[e]}")
    segs.append(f"ProcessDescription：{item['ProcessDescription']}")
    return "，".join(segs)


# =============================
# extractedDoiSet.json 断点续跑（原子写）（保持不变）
# =============================

def load_extracted_dois(extracted_set_path: str) -> set:
    if os.path.exists(extracted_set_path):
        with open(extracted_set_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return set([str(x).strip() for x in data if str(x).strip()])
    return set()


def save_extracted_dois_atomic(extracted_set_path: str, doi_set: set) -> None:
    tmp_path = extracted_set_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(sorted(list(doi_set)), f, ensure_ascii=False, indent=2)
    os.replace(tmp_path, extracted_set_path)


# =============================
# Worker：单篇论文（LLM结构化抽表 + fallback + 工艺按材料写回）
# =============================

def worker_process_one(
    file1: str,
    input_folder: str,
    output_folder: str,
    base_url: str,
    api_key: str,
    model: str,
    ENABLE_STRONG_FALLBACK: bool = True,
    ENABLE_FILL_MISSING_AFTER_STRONG: bool = False,
    ENABLE_FILL_MISSING_AFTER_MAIN: bool = True,
) -> Tuple[str, bool, str]:
    doi = file1[:-5]
    paper_path = os.path.join(input_folder, file1)
    out_path = os.path.join(output_folder, file1)
    err_path = os.path.join(output_folder, doi + ".error.json")

    if os.path.exists(out_path):
        return doi, True, "output_exists"

    try:
        with open(paper_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # 1) 主路径：抽 items
        items = extract_items_with_llm(base_url, api_key, model, data, max_tokens=4096)

        # 2) 主路径仍为空 -> 强 fallback
        if not items:
            if not ENABLE_STRONG_FALLBACK:
                raise RuntimeError("未能抽到任何材料条目，且未启用强 fallback。")
            items = strong_fallback_extract_items_with_llm(base_url, api_key, model, data)
            if not items:
                raise RuntimeError("强 fallback 仍未抽到 items（证据段落可能不足或模型输出异常）。")

            if ENABLE_FILL_MISSING_AFTER_STRONG and need_fill_properties(items, 0.5):
                items = fill_missing_properties_with_llm(base_url, api_key, model, items, data)
        else:
            # 3) 主路径抽到 items，但缺失严重 -> 轻量补全
            if ENABLE_FILL_MISSING_AFTER_MAIN and need_fill_properties(items, zero_ratio_threshold=0.5):
                items = fill_missing_properties_with_llm(base_url, api_key, model, items, data)

        # 4) 工艺：按材料分别抽取并写回（终点=Material）
        process_corpus = build_process_corpus(data)
        tables_md_for_process = build_tables_payload(data.get("tables", []) or [], max_chars=12000)

        materials: List[str] = []
        for it in items:
            m = normalize_material_name(it.get("Material", ""))
            if m:
                materials.append(m)
        materials = list(dict.fromkeys(materials))

        mat_process_map = get_material_process_map(
            base_url=base_url,
            api_key=api_key,
            model=model,
            materials=materials,
            process_corpus=process_corpus,
            tables_md=tables_md_for_process
        )

        # fallback：若某些材料缺失，用 common process 兜底
        common_process = ""
        missing_mats = [m for m in materials if not compact_space(mat_process_map.get(m, ""))]
        if missing_mats:
            common_process = get_common_process_description(base_url, api_key, model, process_corpus)

        # 写回：去掉“以{mat}为对象”前缀（避免把材料放到句首造成“起点错觉”）
        for i, it in enumerate(items):
            it2 = normalize_items_schema(it)
            mat = it2["Material"]

            desc = compact_space(mat_process_map.get(mat, ""))
            if not desc:
                desc = common_process

            # 强制保证：终点=mat
            desc = ensure_process_ends_with_material(desc, mat)

            it2["ProcessDescription"] = desc
            items[i] = it2

        # 5) 输出字符串数组
        strings = [item_to_target_string(it) for it in items]
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(strings, f, ensure_ascii=False, indent=2)

        return doi, True, "ok"

    except Exception as e:
        with open(err_path, "w", encoding="utf-8") as f:
            json.dump({"doi": doi, "file": file1, "error": str(e)}, f, ensure_ascii=False, indent=2)
        return doi, False, str(e)


# =============================
# 主程序：并发 + 断点续跑
# =============================

def main():
    # ===== 按你环境修改 =====
    input_folder = r"E:\Code\datapull\main\work\20260116\xml2jsonRes"
    extracted_set_path = r"E:\Code\datapull\main\knowledge_extraction\extractedDoiSet.json"

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    output_folder = rf"E:\Code\datapull\main\work\20260116\extract_data_{timestamp}"
    os.makedirs(output_folder, exist_ok=True)

    # ===== Ollama / 模型 =====
    base_url = "http://localhost:11434/v1"
    api_key = "ollama"
    model = "gpt-oss:20b-40960"

    # ===== 并发数（建议 2~4）=====
    MAX_WORKERS = 4

    extracted_dois = load_extracted_dois(extracted_set_path)
    files = [f for f in os.listdir(input_folder) if f.endswith(".json")]
    todo_files = sorted([f for f in files if f[:-5] not in extracted_dois])

    progressbar = tqdm(total=len(todo_files), desc="执行进度", colour="white")
    extracted_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [
            executor.submit(
                worker_process_one,
                file1, input_folder, output_folder,
                base_url, api_key, model,
                True,    # ENABLE_STRONG_FALLBACK
                False,   # ENABLE_FILL_MISSING_AFTER_STRONG
                True,    # ENABLE_FILL_MISSING_AFTER_MAIN
            )
            for file1 in todo_files
        ]

        for fut in as_completed(futures):
            doi, ok, _ = fut.result()
            if ok:
                with extracted_lock:
                    extracted_dois.add(doi)
                    save_extracted_dois_atomic(extracted_set_path, extracted_dois)
            progressbar.update(1)

    progressbar.close()
    print("Done")


if __name__ == "__main__":
    main()
