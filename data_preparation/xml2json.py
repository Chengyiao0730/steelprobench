#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
from lxml import etree
import json


# ====== 配置区 ======
INPUT_FOLDER = "E:\Code\datapull\main\work/20260116/xml_downloads"          # 输入文件夹（存放 .xml）
OUTPUT_FOLDER = "E:\Code\datapull\main\work/20260116/xml2jsonRes"       # 输出文件夹（生成 .json）
# ===================


def extract_text(elem):
    if elem is None:
        return ""
    parts = []
    for node in elem.iter():
        if node.text:
            parts.append(node.text)
        if node.tail:
            parts.append(node.tail)
    return ' '.join(' '.join(parts).split())


def extract_section_tree(section_elem):
    title_elems = section_elem.xpath("./*[local-name()='section-title']")
    title = extract_text(title_elems[0]) if title_elems else ""

    paras = []
    for child in section_elem:
        if child.xpath("local-name()") == 'para':
            paras.append(extract_text(child))
        elif child.xpath("local-name()") == 'section':
            break

    subsections = []
    for child in section_elem:
        if child.xpath("local-name()") == 'section':
            subsections.append(extract_section_tree(child))

    return {
        "title": title,
        "text": "\n".join(paras).strip(),
        "subsections": subsections
    }


def get_top_level_sections(root):
    all_sections = root.xpath("//*[local-name()='section']")
    top_sections = []
    for sec in all_sections:
        parent = sec.getparent()
        if parent is None or parent.xpath("local-name()") != 'section':
            top_sections.append(sec)
    return top_sections


def extract_tables(root):
    tables = []
    for table in root.xpath("//*[local-name()='table']"):
        label_elem = table.xpath("./*[local-name()='label']")
        label = extract_text(label_elem[0]) if label_elem else ""

        caption_elem = table.xpath(".//*[local-name()='simple-para']")
        caption = extract_text(caption_elem[0]) if caption_elem else ""

        tgroups = table.xpath("./*[local-name()='tgroup']")
        if not tgroups:
            continue
        tgroup = tgroups[0]

        header = []
        thead = tgroup.xpath("./*[local-name()='thead']")
        if thead:
            rows = thead[0].xpath("./*[local-name()='row']")
            if rows:
                entries = rows[0].xpath("./*[local-name()='entry']")
                header = [extract_text(e) for e in entries]

        rows_data = []
        tbody = tgroup.xpath("./*[local-name()='tbody']")
        if tbody:
            for row in tbody[0].xpath("./*[local-name()='row']"):
                entries = row.xpath("./*[local-name()='entry']")
                cells = [extract_text(e) for e in entries]
                if any(c.strip() for c in cells):
                    rows_data.append(cells)

        tables.append({
            "label": label,
            "caption": caption,
            "header": header,
            "rows": rows_data
        })
    return tables


# ✅ 新增：构建 predata 字段
def build_predata(sections_tree):
    """
    输入：sections_tree（列表，每个元素是顶层章节）
    输出：predata（列表，每个元素是顶级章节及其子章节的合并文本）
    """
    all_numbered = []

    def traverse(sec, prefix):
        # 当前章节编号
        number = prefix
        title = sec["title"]
        text = sec["text"]
        content = f"{number}. {title}".strip()
        if text:
            content += "\n" + text

        all_numbered.append((number, content))

        # 递归子章节
        for i, sub in enumerate(sec["subsections"], start=1):
            traverse(sub, f"{prefix}.{i}")

    # 为每个顶级章节编号（1, 2, 3...）
    for idx, top_sec in enumerate(sections_tree, start=1):
        traverse(top_sec, str(idx))

    # 按顶级编号分组（如 "1", "2"）
    groups = {}
    for number, content in all_numbered:
        top_level = number.split('.')[0]  # "1.2.3" → "1"
        if top_level not in groups:
            groups[top_level] = []
        groups[top_level].append(content)

    # 合并每组为一个字符串，并按数字顺序排序
    predata = []
    for top in sorted(groups.keys(), key=lambda x: int(x) if x.isdigit() else float('inf')):
        predata.append("\n\n".join(groups[top]))

    return predata


def process_xml_file(xml_path, output_dir):
    try:
        with open(xml_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()

        if not content or not content.startswith('<'):
            print(f"⚠️ 跳过无效文件: {xml_path}")
            return

        if not (
            content.lstrip().startswith('<article') or
            content.lstrip().startswith('<root') or
            content.lstrip().startswith('<?xml')
        ):
            content = f"<root>{content}</root>"

        root = etree.fromstring(content.encode('utf-8'))

        top_sections = get_top_level_sections(root)
        sections_tree = [extract_section_tree(sec) for sec in top_sections]
        tables = extract_tables(root)

        # ✅ 构建 predata
        predata = build_predata(sections_tree)

        result = {
            "sections_tree": sections_tree,
            "tables": tables,
            "predata": predata  # ← 新增字段
        }

        base_name = os.path.splitext(os.path.basename(xml_path))[0]
        output_path = os.path.join(output_dir, base_name + ".json")
        if len(predata) >0:
            with open(output_path, 'w', encoding='utf-8') as out_f:
                json.dump(result, out_f, ensure_ascii=False, indent=2)
            def count_all(sec_list):
                return sum(1 + count_all(sec["subsections"]) for sec in sec_list)
            total_secs = count_all(sections_tree)
            print(f"✅ {os.path.basename(xml_path)} → 章节: {total_secs}, 表格: {len(tables)}, predata 条目: {len(predata)}")

    except Exception as e:
        print(f"❌ 处理失败 {xml_path}: {e}")


def main():
    os.makedirs(OUTPUT_FOLDER, exist_ok=True)

    if not os.path.isdir(INPUT_FOLDER):
        print(f"错误: 输入文件夹 '{INPUT_FOLDER}' 不存在！")
        return

    xml_files = [f for f in os.listdir(INPUT_FOLDER) if f.lower().endswith('.xml')]
    if not xml_files:
        print(f"警告: '{INPUT_FOLDER}' 中没有 .xml 文件")
        return

    print(f"📁 找到 {len(xml_files)} 个 XML 文件，开始处理...\n")

    exist_json = set(os.listdir(OUTPUT_FOLDER))
    iii = 0
    for filename in xml_files:
        if f"{filename.split('.xml')[0]}.json" not in exist_json:
            xml_path = os.path.join(INPUT_FOLDER, filename)
            process_xml_file(xml_path, OUTPUT_FOLDER)
            iii += 1
        else:
            print("跳过已生成的xml文件")
    print(f"\n🎉 批量处理完成！结果保存在: {OUTPUT_FOLDER}")
    print(f"\n🎉 新处理了{iii}个xml")


if __name__ == "__main__":
    main()