import os
import sys
import json
import time
import re
import urllib.parse
from typing import List, Set, Optional, Dict, Any, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class ElsevierKeywordToXMLDownloader:
    """
    用 ScienceDirect Search API 通过关键词检索论文（拿 DOI/PII），再用 Article API 下载 XML 全文。

    ✅ 本版本：
    - doiSet.json 存储：不带后缀的“纯 DOI（标准化后）”数组，例如 ["10.1016_S001...", ...]
    - 每成功保存一个新的 DOI XML：实时把“标准化后的 DOI”追加到 set 并写回 doiSet.json（原子写入）
    - 统计“新下载数量”准确：只有本次真的写入新 XML 才计数
      （已存在/仅补记 不计数）
    """

    # ======== （配置区）========
    API_KEY = "3ab31e5cb6a92c98d979190f100513b2"  # 建议用环境变量/配置文件

    OUTPUT_DIR = r"E:\Code\datapull\main\work\20260114\xml_downloads"
    DOWNLOADED_DOI_JSON = r"E:\Code\datapull\main\data_preparation\doiSet.json"  # ✅ 实时更新（纯 DOI norm 数组）

    KEYWORDS = [
        # "CFB Steel"
        # # "Carbide-Free Bainitic Steel",
        # # "Alloyed Steel",
        # # "Hot-Stamped Steel"
        # "Medium-Entropy Steel",
        "Precipitation-Hardened Steel",
        # "Hydrogen-Resistant Steel",
        # "Additively Manufactured Steel",
        # "Gradient Nanostructured Steel"
    # "metallic materials",
    # "metals and alloys",
    # "engineering alloys",
    # "structural metals",
    # "lightweight alloys",
    # "high-entropy alloys (HEA)",

    ]

    # 下载数量控制（统计的是“新下载”数量）
    MAX_TOTAL_DOWNLOADS = 1500
    MAX_PER_KEYWORD = 500

    # 搜索控制
    MAX_RESULTS_PER_KEYWORD = 500
    SEARCH_SCOPE = "title"            # "title" / "abstract" / "all"
    RESTRICT_FIELD = "metallurgy"     # 不想限制就设为 None

    SEARCH_BATCH_SIZE = 50
    SEARCH_SLEEP_S = 0.5

    # 下载控制
    DOWNLOAD_SLEEP_S = 0.15
    VIEW_FULL = False
    # ========================================

    def __init__(self):
        self.search_url = "https://api.elsevier.com/content/search/scidir"
        self.fulltext_pii_url = "https://api.elsevier.com/content/article/pii/{pii}"
        self.fulltext_doi_url = "https://api.elsevier.com/content/article/doi/{doi}"

        os.makedirs(self.OUTPUT_DIR, exist_ok=True)

        self.session = self._create_session()

        self.search_headers = {
            "X-ELS-APIKey": self.API_KEY,
            "Accept": "application/json"
        }
        self.xml_headers = {
            "X-ELS-APIKey": self.API_KEY,
            "Accept": "text/xml"
        }

        # ✅ 加载：标准化 DOI 集合（与 XML 文件名一致的那套规则）
        self.downloaded_doi_norm_set = self._load_downloaded_doi_norm_set(self.DOWNLOADED_DOI_JSON)

    def _create_session(self) -> requests.Session:
        session = requests.Session()
        session.trust_env = False
        retry_strategy = Retry(
            total=5,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        return session

    # -------------------------
    # DOI 标准化 / 去重文件读写
    # -------------------------
    @staticmethod
    def _clean_filename(s: str) -> str:
        return re.sub(r'[\\/*?:"<>|]', "_", s)

    def _doi_to_norm(self, doi: str) -> str:
        """
        把 DOI 标准化成与你 XML 文件名一致的“norm doi”：
        - / 和 \\ => _
        - 再做文件名安全化
        """
        doi = (doi or "").strip()
        return self._clean_filename(doi.replace("/", "_").replace("\\", "_"))

    def _load_downloaded_doi_norm_set(self, json_path: str) -> Set[str]:
        """
        从 doiSet.json 加载标准化 DOI set。
        json 内容应为：["10.1016_....", ...]
        兼容：{"dois":[...]} 但最终都会转成 set[str]
        """
        if not json_path or not os.path.isfile(json_path):
            print(f"⚠️ 未找到 doiSet.json，将在首次成功下载后创建：{json_path}")
            return set()

        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)

            items: List[str] = []
            if isinstance(data, list):
                items = [x for x in data if isinstance(x, str) and x.strip()]
            elif isinstance(data, dict) and isinstance(data.get("dois"), list):
                items = [x for x in data["dois"] if isinstance(x, str) and x.strip()]
            else:
                print(f"⚠️ doiSet.json 结构不识别：{type(data)}，将当作空集合处理。")
                return set()

            s = set(items)
            print(f"✅ 已从 {json_path} 加载去重数量：{len(s)}（纯 DOI norm 数组）")
            return s

        except Exception as e:
            print(f"⚠️ 读取 {json_path} 失败：{e}（将当作空集合处理）")
            return set()

    def _atomic_write_json_list(self, path: str, items: List[str]) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    def _save_doi_set_file(self) -> None:
        """
        把当前 set 写回 doiSet.json（纯数组）。
        """
        items = sorted(self.downloaded_doi_norm_set)
        try:
            self._atomic_write_json_list(self.DOWNLOADED_DOI_JSON, items)
        except Exception as e:
            print(f"⚠️ 写回 doiSet.json 失败：{e}")

    def _record_downloaded_doi_realtime(self, doi: str) -> None:
        """
        ✅ 每次成功保存 DOI XML 后调用：
        - DOI -> norm doi
        - add 到 set
        - 立刻写回 doiSet.json（纯数组）
        """
        norm = self._doi_to_norm(doi)
        if not norm:
            return
        if norm in self.downloaded_doi_norm_set:
            return

        self.downloaded_doi_norm_set.add(norm)
        self._save_doi_set_file()
        print(f"📝 已实时更新 doiSet.json：+ {norm}")

    # -------------------------
    # 搜索与下载逻辑
    # -------------------------
    def _build_query(self, keyword: str) -> str:
        keyword = keyword.strip()
        if self.SEARCH_SCOPE == "title":
            base_query = f"TITLE({keyword})"
        elif self.SEARCH_SCOPE == "abstract":
            base_query = f"abstract({keyword})"
        else:
            base_query = f"title({keyword}) OR abstract({keyword})"

        if self.RESTRICT_FIELD:
            return f"({self.RESTRICT_FIELD}) AND ({base_query})"
        return base_query

    def search_papers(self, keyword: str, max_results: int) -> List[Dict[str, Any]]:
        all_entries: List[Dict[str, Any]] = []
        start = 0
        query = self._build_query(keyword)

        print(f"搜索范围: {self.SEARCH_SCOPE} | 关键词: {keyword}" +
              (f" | 限制领域: {self.RESTRICT_FIELD}" if self.RESTRICT_FIELD else ""))

        while True:
            current_batch = min(self.SEARCH_BATCH_SIZE, max_results - start)
            if current_batch <= 0:
                break

            params = {"query": query, "count": current_batch, "start": start}

            try:
                print(f"[SEARCH] start={start}, count={current_batch}")
                resp = self.session.get(self.search_url, headers=self.search_headers, params=params, timeout=60)
                resp.raise_for_status()
            except requests.exceptions.RequestException as e:
                print(f"❌ [SEARCH] 请求失败: {e}")
                break

            remaining = resp.headers.get("X-RateLimit-Remaining")
            if remaining and remaining.isdigit() and int(remaining) < 5:
                reset_time = resp.headers.get("X-RateLimit-Reset")
                try:
                    reset_time = int(reset_time) if reset_time else int(time.time() + 60)
                    sleep_time = max(0, reset_time - time.time() + 1)
                    print(f"⏳ 速率限制预警，休眠 {sleep_time:.1f}s")
                    time.sleep(sleep_time)
                except Exception:
                    pass

            data = resp.json()
            batch = data.get("search-results", {}).get("entry", [])
            if not batch:
                print("ℹ️ [SEARCH] 无更多结果。")
                break

            all_entries.extend(batch)
            start += current_batch
            time.sleep(self.SEARCH_SLEEP_S)

        return all_entries

    def download_xml_by_doi(self, doi: str) -> Tuple[Optional[str], bool]:
        """
        通过 DOI 下载 XML。
        返回 (path, is_new_download)
        - is_new_download=True：本次真的下载并写入新 XML（计数用）
        - is_new_download=False：跳过/失败/仅补记，不计数
        """
        doi = (doi or "").strip()
        if not doi:
            return None, False

        norm = self._doi_to_norm(doi)

        # 1) doiSet 去重：已记录 => 不是新下载
        if norm in self.downloaded_doi_norm_set:
            print(f"⏭️ [SKIP] 已在 doiSet.json 中记录（norm 匹配）：{norm}  <=  DOI: {doi}")
            return None, False

        out_path = os.path.join(self.OUTPUT_DIR, f"{norm}.xml")

        # 2) 输出目录去重：XML 已存在 => 不是新下载，但要补记 doiSet
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            print(f"⏭️ [SKIP] XML 已存在：{out_path}")
            self._record_downloaded_doi_realtime(doi)
            return out_path, False

        encoded = urllib.parse.quote(doi, safe="")
        url = self.fulltext_doi_url.format(doi=encoded)

        params = {}
        if self.VIEW_FULL:
            params["view"] = "FULL"

        print(f"[DL-DOI] {doi}")
        try:
            resp = self.session.get(url, headers=self.xml_headers, params=params, timeout=120)
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"❌ [DL-DOI] 失败：{e}")
            return None, False

        content_type = (resp.headers.get("content-type") or "").lower()
        if "xml" not in content_type and not resp.content.strip().startswith(b"<?xml"):
            err_path = out_path.replace(".xml", "_error.html")
            with open(err_path, "wb") as f:
                f.write(resp.content)
            print(f"⚠️ [DL-DOI] 返回非 XML（{content_type}），已保存：{err_path}")
            return None, False

        # ✅ 真正写入新文件
        with open(out_path, "wb") as f:
            f.write(resp.content)
        print(f"✅ [DL-DOI] 保存成功：{out_path}")

        # ✅ 实时更新 doiSet（确保中断也能续）
        self._record_downloaded_doi_realtime(doi)

        return out_path, True

    def download_xml_by_pii(self, pii: str) -> Tuple[Optional[str], bool]:
        """
        通过 PII 下载 XML（兜底用）。
        返回 (path, is_new_download)
        注：无 DOI 时不写 doiSet.json（避免污染 DOI 集合）。
        """
        pii = (pii or "").strip()
        if not pii:
            return None, False

        safe = self._clean_filename(pii)
        out_path = os.path.join(self.OUTPUT_DIR, f"PII_{safe}.xml")

        # 已存在则不算新下载
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            print(f"⏭️ [SKIP] PII XML 已存在：{out_path}")
            return out_path, False

        url = self.fulltext_pii_url.format(pii=pii)
        params = {}
        if self.VIEW_FULL:
            params["view"] = "FULL"

        print(f"[DL-PII] {pii}")
        try:
            resp = self.session.get(url, headers=self.xml_headers, params=params, timeout=120)
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            print(f"❌ [DL-PII] 失败：{e}")
            return None, False

        content_type = (resp.headers.get("content-type") or "").lower()
        if "xml" not in content_type and not resp.content.strip().startswith(b"<?xml"):
            err_path = os.path.join(self.OUTPUT_DIR, f"{safe}_error.html")
            with open(err_path, "wb") as f:
                f.write(resp.content)
            print(f"⚠️ [DL-PII] 返回非 XML（{content_type}），已保存：{err_path}")
            return None, False

        with open(out_path, "wb") as f:
            f.write(resp.content)
        print(f"✅ [DL-PII] 保存成功：{out_path}")

        return out_path, True

    def run(self):
        if not self.API_KEY or not self.API_KEY.strip():
            print("❌ 请先在配置区设置 API_KEY")
            sys.exit(1)

        if not self.KEYWORDS:
            print("❌ KEYWORDS 为空，请在配置区填写关键词列表")
            return

        total_new = 0
        seen_this_run: Set[str] = set()

        for kw_idx, keyword in enumerate([k.strip() for k in self.KEYWORDS if k.strip()], 1):
            if total_new >= self.MAX_TOTAL_DOWNLOADS:
                break

            print("\n" + "=" * 90)
            print(f"[{kw_idx}/{len(self.KEYWORDS)}] 开始处理关键词：{keyword}")
            print("=" * 90)

            papers = self.search_papers(keyword, self.MAX_RESULTS_PER_KEYWORD)
            total_found = len(papers)
            print(f"找到论文条目：{total_found}（最多检索 {self.MAX_RESULTS_PER_KEYWORD}）")

            per_kw_new = 0

            for i, paper in enumerate(papers, 1):
                if total_new >= self.MAX_TOTAL_DOWNLOADS:
                    break
                if per_kw_new >= self.MAX_PER_KEYWORD:
                    break

                doi = paper.get("prism:doi")
                pii = paper.get("pii")

                # 优先 DOI
                if isinstance(doi, str) and doi.strip():
                    doi = doi.strip()
                    if doi in seen_this_run:
                        continue
                    seen_this_run.add(doi)

                    print(f"\n[{keyword}] {i}/{total_found} | DOI: {doi}")
                    _, is_new = self.download_xml_by_doi(doi)
                    if is_new:
                        total_new += 1
                        per_kw_new += 1
                else:
                    # DOI 没有的话，尝试 PII
                    if not pii:
                        print(f"⏭️ {i}/{total_found} 无 DOI/PII，跳过")
                        continue
                    print(f"\n[{keyword}] {i}/{total_found} | 无 DOI，尝试 PII: {pii}")
                    _, is_new = self.download_xml_by_pii(pii)
                    if is_new:
                        total_new += 1
                        per_kw_new += 1

                time.sleep(self.DOWNLOAD_SLEEP_S)

            print(f"✅ 关键词 '{keyword}' 完成：新下载 {per_kw_new} 篇（总新下载累计 {total_new}）")
            time.sleep(1.5)

        print("\n" + "=" * 90)
        print(f"全部完成：本次新下载 {total_new} 篇（总上限 {self.MAX_TOTAL_DOWNLOADS}）")
        print("=" * 90)


if __name__ == "__main__":
    downloader = ElsevierKeywordToXMLDownloader()
    downloader.run()
