import csv
import random

import requests
import urllib.parse
import os
import sys

# ===== 配置区 =====
API_KEY = "dadsadsa"  # ←←← 请替换为你的 Elsevier API Key
OUTPUT_DIR = rf"E:\Code\datapull\main\data_preparation\1"
# ==================

os.makedirs(OUTPUT_DIR, exist_ok=True)

def download_xml_by_doi(doi):
    """
    根据 DOI 从 Elsevier 下载 XML 全文
    """
    # 对 DOI 进行 URL 编码（处理特殊字符如 /）
    encoded_doi = urllib.parse.quote(doi, safe='')
    url = f"https://api.elsevier.com/content/article/doi/{encoded_doi}"

    headers = {
        "X-ELS-APIKey": API_KEY,
        "Accept": "text/xml"
    }

    print(f"📡 正在请求: {url}")
    response = requests.get(url, headers=headers)

    # 检查 HTTP 状态
    if response.status_code == 200:
        content_type = response.headers.get('content-type', '').lower()
        if 'xml' in content_type or response.content.strip().startswith(b'<?xml'):
            # 生成安全的文件名（替换非法字符）
            safe_doi = doi.replace('/', '_').replace('\\', '_')
            filename = f"{safe_doi}.xml"
            filepath = os.path.join(OUTPUT_DIR, filename)

            with open(filepath, 'wb') as f:
                f.write(response.content)
            print(f"✅ 成功下载 XML → {filepath}")
            return filepath
        else:
            print(f"⚠️ 警告: 返回内容不是 XML（Content-Type: {content_type}）")
            print("   可能原因：无全文权限，返回了封面页或错误信息。")
            # 可选：保存错误内容用于调试
            safe_doi = doi.replace('/', '_')
            with open(os.path.join(OUTPUT_DIR, f"{safe_doi}_error.html"), 'wb') as f:
                f.write(response.content)
            return None
    else:
        print(f"❌ 请求失败: HTTP {response.status_code}")
        print("响应内容预览:")
        print(response.text[:500])
        return None


def extract_first_column(csv_file_path, has_header=False):
    """
    读取 CSV 文件，返回第一列数据组成的列表。

    参数:
        csv_file_path (str): CSV 文件路径
        has_header (bool): 是否跳过第一行（表头）

    返回:
        list: 第一列的所有值
    """
    if not os.path.isfile(csv_file_path):
        print(f"❌ 文件不存在: {csv_file_path}")
        return []

    first_column = []
    try:
        with open(csv_file_path, 'r', encoding='utf-8', newline='') as f:
            reader = csv.reader(f)
            rows = list(reader)
            if not rows:
                print("⚠️  CSV 文件为空")
                return []

            start_index = 1 if has_header else 0
            for row in rows[start_index:]:
                if row:  # 确保行非空
                    first_column.append(row[0])
                else:
                    first_column.append('')  # 空行则添加空字符串
        return first_column

    except Exception as e:
        print(f"❌ 读取 CSV 时出错: {e}")
        return []

# ===== 主程序 =====
if __name__ == "__main__":
    if API_KEY == "YOUR_API_KEY_HERE":
        print("❌ 请先在脚本中设置你的 Elsevier API Key！")
        print("获取地址: https://dev.elsevier.com/")
        sys.exit(1)
    # ✅ 修改这里指定你的 CSV 文件路径
    csv_file = "E:\Code\datapull\main\compare_result\data.csv"  # ←←← 改成你的文件名
    skip_header = False  # ←←← 如果第一行是表头，设为 True
    data = extract_first_column(csv_file, has_header=skip_header)
    # ✅ 去重并保留顺序
    dois = list(dict.fromkeys(data))
    dois_ = dois[0:6000]
    for doi in dois_:
        print(f"📥 准备下载 DOI: {doi}")
        download_xml_by_doi(doi)