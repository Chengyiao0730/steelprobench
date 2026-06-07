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


class ElsevierScopusKeywordToXMLDownloader:
    """
    用 Scopus Search API 通过关键词检索论文（拿 DOI + PII），再用 Elsevier Article API 下载 XML 全文。

    ✅ 解决你提出的两点：
    1) doiSet.json 只按 DOI(norm) 维护：所以必须拿到 DOI 才能计入/写入 doiSet.json（PII-only 条目将跳过）
    2) 即便用 PII 下载，也用 DOI(norm) 命名 XML 文件：{doi_norm}.xml

    ✅ 附加改进：
    - 大量 DOI 404：正常（Scopus 收录非 Elsevier 文章）；增加 doiNotFound.json 黑名单避免重复 404
    - 优先 PII 下载（更可能命中 Elsevier 平台 XML），但前提是 entry 同时具备 DOI
    - 兼容 X-RateLimit-Reset 为 epoch 或 delta
    - 可选禁用系统代理（避免公司代理导致卡顿）

    ✅ 本次按你的要求新增：
    1) 在搜索 query 中加入“领域限制”（Scopus 的 LIMIT-TO(SUBJAREA,...)）
    2) 在 KEYWORDS 里加入三个力学性能关键词：张力、屈服值、伸长率
    """

    # ================== 配置区 ==================
    API_KEY = "69e483f4961b0eda8deb56c8bde2a06b"  # 建议放环境变量

    OUTPUT_DIR = r"E:\Code\datapull\main\work\20260116\xml_downloads"
    DOWNLOADED_DOI_JSON = r"E:\Code\datapull\main\data_preparation\doiSet.json"
    NOT_FOUND_DOI_JSON = r"E:\Code\datapull\main\data_preparation\doiNotFound.json"  # ✅ 404 的 DOI(norm) 黑名单
    KEYWORDS = [
        # 1) Core / 基础核心关键词
        "tensile test",
        "tensile testing",
        "tensile properties",
        "tensile strength",
        "ultimate tensile strength",
        "UTS",
        "yield strength",
        "yield stress",
        "0.2% offset yield strength",
        "0.2% proof stress",
        "proof stress",
        "elongation",
        "elongation at break",
        "ductility",
        "engineering stress–strain curve",
        "engineering stress-strain curve",
        "true stress–true strain",
        "true stress-true strain",
        "plastic deformation",

        # 2) Combined / 组合检索
        "tensile properties AND yield strength AND elongation",
        "tensile strength AND 0.2% proof stress AND ductility",
        "stress–strain behavior AND yield point AND elongation to failure",
        "stress-strain behavior AND yield point AND elongation to failure",
        "yield ratio AND tensile strength AND elongation",
        "work hardening AND yield strength AND uniform elongation",
        "uniform elongation AND total elongation AND tensile test",

        # 3) Elongation-related / 伸长率细分
        "uniform elongation",
        "total elongation",
        "fracture elongation",
        "elongation to failure",
        "strain to failure",
        "reduction of area",
        "RA",
        "necking",

        # 4) Yield-related / 屈服细分
        "yield point",
        "upper yield point",
        "lower yield point",
        "yield plateau",
        "0.2% offset method",
        "Lüders bands",
        "Luders bands",

        # 5) Material + properties templates / 材料+指标模板
        "steel tensile properties yield strength elongation",
        "stainless steel yield strength ductility tensile",
        "aluminum alloy tensile properties 0.2% proof stress elongation",
        "titanium alloy tensile behavior yield stress elongation",
        "magnesium alloy tensile ductility yield strength",
        "high-strength steel yield ratio elongation",
        "dual-phase steel tensile properties yield strength uniform elongation",
        "TRIP steel tensile behavior yield plateau elongation",
        "TWIP steel high ductility yield strength tensile",
        "nickel-based superalloy tensile properties yield strength elongation",

        # 6) Mechanism / 工艺-性能关系关键词
        "microstructure–property relationship",
        "microstructure-property relationship",
        "grain size effect",
        "Hall–Petch",
        "Hall-Petch",
        "precipitation strengthening",
        "solid solution strengthening",
        "dislocation density",
        "texture",
        "heat treatment AND tensile properties",
        "annealing AND yield strength AND elongation",
        "cold rolling AND tensile properties",
        "strain hardening exponent",
        "n-value",
        "strain-rate sensitivity",
        "strain rate sensitivity",
        "m-value",

        # 7) Ready-to-paste advanced queries / 可直接复制的高级检索串
        '"tensile properties" AND "0.2% proof stress" AND "total elongation" AND steel',
        '"true stress true strain" AND "uniform elongation" AND "necking" AND alloy',
        '"yield plateau" AND "Lüders bands" AND "elongation" AND low carbon steel',
        '"yield plateau" AND "Luders bands" AND "elongation" AND low carbon steel',
        '"yield ratio" AND "ultimate tensile strength" AND "ductility" AND "high strength steel"',
        '"microstructure" AND "yield strength" AND "elongation" AND "heat treatment" AND "aluminum alloy"',

    ]

    # 下载数量控制（统计的是“新下载”数量）
    MAX_TOTAL_DOWNLOADS = 10000
    MAX_PER_KEYWORD = 200

    # 搜索控制
    MAX_RESULTS_PER_KEYWORD = 300
    SEARCH_BATCH_SIZE = 50
    SEARCH_SLEEP_S = 0.15

    # Scopus query 范围：title / abstract / all（TITLE-ABS）
    SEARCH_SCOPE = "title"

    # 可选：文献类型过滤（Scopus 常用：DOCTYPE(ar)=Article）
    DOC_TYPE_FILTER = "DOCTYPE(ar)"   # 不想限制就设为 None

    # ✅ 领域限制（按你的要求：在搜索 query 中加入领域限制）
    # Scopus Advanced Search 常用写法：LIMIT-TO(SUBJAREA,"MATE")
    # 你要限制到哪些领域，就把 code 放这里（不想限制就设为 None 或 []）
    DOMAIN_SUBJAREAS = ["materials"]

    # ✅ 强力过滤：必须同时有 DOI（用于命名+doiSet）；
    # 如果 ALSO_REQUIRE_PII=True，则只抓 (DOI + PII) 的条目（Elsevier 命中率更高）
    REQUIRE_DOI = True
    ALSO_REQUIRE_PII = False  # 你如果想进一步减少 404，可以设 True

    # 下载控制
    DOWNLOAD_SLEEP_S = 0.15
    VIEW_FULL = False

    # 网络：公司环境经常被系统代理卡住时建议 True
    DISABLE_ENV_PROXY = True
    # ===========================================

    def __init__(self):
        self.search_url = "https://api.elsevier.com/content/search/scopus"
        self.fulltext_pii_url = "https://api.elsevier.com/content/article/pii/{pii}"
        self.fulltext_doi_url = "https://api.elsevier.com/content/article/doi/{doi}"

        os.makedirs(self.OUTPUT_DIR, exist_ok=True)

        self.session = self._create_session()

        self.search_headers = {
            "X-ELS-APIKey": self.API_KEY,
            "Accept": "application/json",
        }
        self.xml_headers = {
            "X-ELS-APIKey": self.API_KEY,
            "Accept": "text/xml",
        }

        self.downloaded_doi_norm_set = self._load_norm_set(self.DOWNLOADED_DOI_JSON, name="doiSet.json(成功下载)")
        self.not_found_doi_norm_set = self._load_norm_set(self.NOT_FOUND_DOI_JSON, name="doiNotFound.json(404黑名单)")

    # -------------------------
    # requests session / retry
    # -------------------------
    def _create_session(self) -> requests.Session:
        session = requests.Session()
        if self.DISABLE_ENV_PROXY:
            session.trust_env = False

        retry_strategy = Retry(
            total=3,
            connect=3,
            read=3,
            status=3,
            backoff_factor=0.6,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET"],
            respect_retry_after_header=True,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("https://", adapter)
        return session

    # -------------------------
    # JSON(set) 读写
    # -------------------------
    @staticmethod
    def _clean_filename(s: str) -> str:
        return re.sub(r'[\\/*?:"<>|]', "_", s)

    def _doi_to_norm(self, doi: str) -> str:
        doi = (doi or "").strip()
        return self._clean_filename(doi.replace("/", "_").replace("\\", "_"))

    def _load_norm_set(self, json_path: str, name: str) -> Set[str]:
        if not json_path or not os.path.isfile(json_path):
            print(f"⚠️ 未找到 {name}，将按空集合开始：{json_path}")
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
                print(f"⚠️ {name} 结构不识别：{type(data)}，将当作空集合处理。")
                return set()

            s = set(items)
            print(f"✅ 已从 {json_path} 加载 {name} 数量：{len(s)}")
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

    def _save_norm_set_file(self, path: str, s: Set[str]) -> None:
        self._atomic_write_json_list(path, sorted(s))

    def _record_downloaded_doi_realtime(self, doi: str) -> None:
        norm = self._doi_to_norm(doi)
        if not norm or norm in self.downloaded_doi_norm_set:
            return
        self.downloaded_doi_norm_set.add(norm)
        try:
            self._save_norm_set_file(self.DOWNLOADED_DOI_JSON, self.downloaded_doi_norm_set)
            print(f"📝 已实时更新 doiSet.json：+ {norm}")
        except Exception as e:
            print(f"⚠️ 写回 doiSet.json 失败：{e}")

    def _record_not_found_doi_realtime(self, doi: str) -> None:
        norm = self._doi_to_norm(doi)
        if not norm or norm in self.not_found_doi_norm_set:
            return
        self.not_found_doi_norm_set.add(norm)
        try:
            self._save_norm_set_file(self.NOT_FOUND_DOI_JSON, self.not_found_doi_norm_set)
            print(f"🧱 已记录 404 DOI（下次跳过）：{norm}")
        except Exception as e:
            print(f"⚠️ 写回 doiNotFound.json 失败：{e}")

    # -------------------------
    # 限流处理：兼容 reset=epoch 或 delta
    # -------------------------
    def _handle_rate_limit_if_needed(self, resp: requests.Response) -> None:
        remaining = resp.headers.get("X-RateLimit-Remaining")
        reset_raw = resp.headers.get("X-RateLimit-Reset")

        if remaining and remaining.isdigit() and int(remaining) < 5:
            now = time.time()
            sleep_time = 60.0
            if reset_raw and reset_raw.isdigit():
                r = int(reset_raw)
                if r > now + 300:
                    sleep_time = max(0.0, r - now) + 1.0
                else:
                    sleep_time = float(r) + 1.0
            print(f"⏳ 速率限制预警，休眠 {sleep_time:.1f}s (remaining={remaining}, reset={reset_raw})")
            time.sleep(sleep_time)

    # -------------------------
    # Scopus query
    # -------------------------
    @staticmethod
    def _escape_scopus_term(s: str) -> str:
        s = (s or "").strip()
        return s.replace('"', '\\"')

    def _build_scopus_query(self, keyword: str) -> str:
        kw = self._escape_scopus_term(keyword)

        if self.SEARCH_SCOPE == "title":
            base = f'TITLE("{kw}")'
        elif self.SEARCH_SCOPE == "abstract":
            base = f'ABS("{kw}")'
        else:
            base = f'TITLE-ABS("{kw}")'

        parts = [base]

        if self.DOC_TYPE_FILTER:
            parts.append(self.DOC_TYPE_FILTER)

        # ✅ 领域限制：用 LIMIT-TO(SUBJAREA,"xxx") 显式加入 query
        # 不想限制：把 DOMAIN_SUBJAREAS 设为 [] 或 None
        if self.DOMAIN_SUBJAREAS:
            domain_expr = " OR ".join([f'LIMIT-TO(SUBJAREA,"{x}")' for x in self.DOMAIN_SUBJAREAS])
            parts.append(f"({domain_expr})")

        return " AND ".join([p for p in parts if p])

    # -------------------------
    # 搜索：Scopus → entries
    # -------------------------
    def search_scopus(self, keyword: str, max_results: int) -> List[Dict[str, Any]]:
        all_entries: List[Dict[str, Any]] = []
        start = 0
        query = self._build_scopus_query(keyword)

        print(f"搜索范围: {self.SEARCH_SCOPE} | 关键词: {keyword}")
        print(f"Scopus query: {query}")

        while True:
            if start >= max_results:
                break

            current_batch = min(self.SEARCH_BATCH_SIZE, max_results - start)
            params = {"query": query, "count": current_batch, "start": start}

            try:
                print(f"[SEARCH] start={start}, count={current_batch}")
                resp = self.session.get(
                    self.search_url,
                    headers=self.search_headers,
                    params=params,
                    timeout=(10, 30),
                )
                self._handle_rate_limit_if_needed(resp)

                if resp.status_code >= 400:
                    print(f"❌ [SEARCH] HTTP {resp.status_code}")
                    print("❌ [SEARCH] body (first 800 chars):", resp.text[:800])
                    resp.raise_for_status()

            except requests.exceptions.RequestException as e:
                print(f"❌ [SEARCH] 请求失败: {e}")
                break

            data = resp.json()
            batch = data.get("search-results", {}).get("entry", [])
            if not batch:
                print("ℹ️ [SEARCH] 无更多结果。")
                break

            # ✅ 过滤：必须有 DOI
            if self.REQUIRE_DOI:
                batch = [e for e in batch if isinstance(e.get("prism:doi"), str) and e["prism:doi"].strip()]

            # ✅ 可选：进一步要求 PII（更接近 Elsevier 平台，减少 DOI 404）
            if self.ALSO_REQUIRE_PII:
                batch = [e for e in batch if isinstance(e.get("pii"), str) and e["pii"].strip()]

            all_entries.extend(batch)
            start += current_batch
            time.sleep(self.SEARCH_SLEEP_S)

        return all_entries

    # -------------------------
    # 下载：用 DOI(norm) 命名，不管 DOI/PII 哪种下载方式
    # -------------------------
    def _output_path_for_doi(self, doi: str) -> str:
        norm = self._doi_to_norm(doi)
        return os.path.join(self.OUTPUT_DIR, f"{norm}.xml")

    def download_xml_by_pii_with_doi_name(self, doi: str, pii: str) -> Tuple[Optional[str], bool]:
        """
        用 PII 下载，但文件名使用 DOI(norm).xml，并且成功后更新 doiSet.json。
        """
        doi = (doi or "").strip()
        pii = (pii or "").strip()
        if not doi or not pii:
            return None, False

        norm = self._doi_to_norm(doi)

        # 成功去重
        if norm in self.downloaded_doi_norm_set:
            print(f"⏭️ [SKIP] doiSet 已记录：{norm}  <= DOI: {doi}")
            return None, False

        out_path = self._output_path_for_doi(doi)

        # 文件存在则补记 doiSet（不计入新下载）
        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            print(f"⏭️ [SKIP] XML 已存在：{out_path}")
            self._record_downloaded_doi_realtime(doi)
            return out_path, False

        url = self.fulltext_pii_url.format(pii=pii)
        params = {}
        if self.VIEW_FULL:
            params["view"] = "FULL"

        print(f"[DL-PII] DOI={doi} | PII={pii} -> {out_path}")
        try:
            resp = self.session.get(url, headers=self.xml_headers, params=params, timeout=(10, 120))
            self._handle_rate_limit_if_needed(resp)

            if resp.status_code >= 400:
                print(f"❌ [DL-PII] HTTP {resp.status_code}")
                print("❌ [DL-PII] body (first 800 chars):", resp.text[:800])
                resp.raise_for_status()

        except requests.exceptions.RequestException as e:
            print(f"❌ [DL-PII] 失败：{e}")
            return None, False

        content_type = (resp.headers.get("content-type") or "").lower()
        if "xml" not in content_type and not resp.content.strip().startswith(b"<?xml"):
            err_path = out_path.replace(".xml", "_error.html")
            with open(err_path, "wb") as f:
                f.write(resp.content)
            print(f"⚠️ [DL-PII] 返回非 XML（{content_type}），已保存：{err_path}")
            return None, False

        # ✅ 真正写入新文件
        with open(out_path, "wb") as f:
            f.write(resp.content)
        print(f"✅ [DL-PII] 保存成功：{out_path}")

        # ✅ 成功后实时更新 doiSet
        self._record_downloaded_doi_realtime(doi)

        return out_path, True

    def download_xml_by_doi(self, doi: str) -> Tuple[Optional[str], bool]:
        """
        用 DOI 下载 XML，文件名使用 DOI(norm).xml，成功后更新 doiSet.json。
        404 会写入 doiNotFound.json 黑名单。
        """
        doi = (doi or "").strip()
        if not doi:
            return None, False

        norm = self._doi_to_norm(doi)

        if norm in self.not_found_doi_norm_set:
            print(f"⏭️ [SKIP] DOI 曾 404（黑名单）：{norm}")
            return None, False

        if norm in self.downloaded_doi_norm_set:
            print(f"⏭️ [SKIP] doiSet 已记录：{norm}  <= DOI: {doi}")
            return None, False

        out_path = self._output_path_for_doi(doi)

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
            resp = self.session.get(url, headers=self.xml_headers, params=params, timeout=(10, 120))
            self._handle_rate_limit_if_needed(resp)

            if resp.status_code == 404:
                print(f"❌ [DL-DOI] HTTP 404 (RESOURCE_NOT_FOUND) DOI={doi}")
                print("❌ [DL-DOI] body (first 200 chars):", resp.text[:200])
                self._record_not_found_doi_realtime(doi)
                return None, False

            if resp.status_code >= 400:
                print(f"❌ [DL-DOI] HTTP {resp.status_code}")
                print("❌ [DL-DOI] body (first 800 chars):", resp.text[:800])
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

        with open(out_path, "wb") as f:
            f.write(resp.content)
        print(f"✅ [DL-DOI] 保存成功：{out_path}")

        self._record_downloaded_doi_realtime(doi)
        return out_path, True

    # -------------------------
    # 主流程
    # -------------------------
    def run(self):
        if not self.API_KEY or not self.API_KEY.strip():
            print("❌ 请先设置 API_KEY")
            sys.exit(1)

        keywords = [k.strip() for k in self.KEYWORDS if k and k.strip()]
        if not keywords:
            print("❌ KEYWORDS 为空")
            return

        total_new = 0
        seen_doi_this_run: Set[str] = set()

        for kw_idx, keyword in enumerate(keywords, 1):
            if total_new >= self.MAX_TOTAL_DOWNLOADS:
                break

            print("\n" + "=" * 90)
            print(f"[{kw_idx}/{len(keywords)}] 开始处理关键词：{keyword}")
            print("=" * 90)

            entries = self.search_scopus(keyword, self.MAX_RESULTS_PER_KEYWORD)
            total_found = len(entries)
            print(f"找到 Scopus 条目：{total_found}（最多检索 {self.MAX_RESULTS_PER_KEYWORD}）")

            per_kw_new = 0

            for i, entry in enumerate(entries, 1):
                if total_new >= self.MAX_TOTAL_DOWNLOADS:
                    break
                if per_kw_new >= self.MAX_PER_KEYWORD:
                    break

                doi = entry.get("prism:doi")
                pii = entry.get("pii")

                if not (isinstance(doi, str) and doi.strip()):
                    print(f"⏭️ {i}/{total_found} 无 DOI（无法命名/无法写 doiSet），跳过")
                    continue
                doi = doi.strip()

                if doi in seen_doi_this_run:
                    continue
                seen_doi_this_run.add(doi)

                # ✅ 先用 PII（如果有），但命名/记录都用 DOI(norm)
                if isinstance(pii, str) and pii.strip():
                    pii = pii.strip()
                    print(f"\n[{keyword}] {i}/{total_found} | DOI: {doi} | PII: {pii} (preferred)")
                    _, is_new = self.download_xml_by_pii_with_doi_name(doi=doi, pii=pii)
                    if is_new:
                        total_new += 1
                        per_kw_new += 1
                    time.sleep(self.DOWNLOAD_SLEEP_S)
                    continue

                # ✅ 无 PII 才用 DOI 直下（但可能 404 多）
                print(f"\n[{keyword}] {i}/{total_found} | DOI: {doi} | 无 PII，尝试 DOI 下载")
                _, is_new = self.download_xml_by_doi(doi)
                if is_new:
                    total_new += 1
                    per_kw_new += 1

                time.sleep(self.DOWNLOAD_SLEEP_S)

            print(f"✅ 关键词 '{keyword}' 完成：新下载 {per_kw_new} 篇（总新下载累计 {total_new}）")
            time.sleep(1.0)

        print("\n" + "=" * 90)
        print(f"全部完成：本次新下载 {total_new} 篇（总上限 {self.MAX_TOTAL_DOWNLOADS}）")
        print("=" * 90)


if __name__ == "__main__":
    downloader = ElsevierScopusKeywordToXMLDownloader()
    downloader.run()
