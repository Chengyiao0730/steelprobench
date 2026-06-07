#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
大规模数据去重系统（优化版）
专为数十万至百万级别条目设计
采用分块处理、增量去重、数据库索引等优化策略
"""

import json
import os
import sqlite3
import hashlib
from typing import List, Dict, Tuple, Iterator
from collections import defaultdict
from tqdm import tqdm
import re
import pickle
from datetime import datetime
import gc


class LargeScaleMaterialDeduplicator:
    """大规模材料条目去重器（内存优化版）"""
    
    def __init__(self, 
                 input_folder: str,
                 output_folder: str = None,
                 similarity_threshold: float = 0.90,
                 batch_size: int = 1000,
                 cache_dir: str = None):
        """
        初始化大规模去重器
        
        Args:
            input_folder: 输入文件夹路径
            output_folder: 输出文件夹路径
            similarity_threshold: 相似度阈值
            batch_size: 批处理大小（建议 500-2000）
            cache_dir: 缓存目录（用于存储中间结果）
        """
        self.input_folder = input_folder
        
        if output_folder is None:
            timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            self.output_folder = f"{input_folder}_deduplicated_large_{timestamp}"
        else:
            self.output_folder = output_folder
            
        os.makedirs(self.output_folder, exist_ok=True)
        
        self.similarity_threshold = similarity_threshold
        self.batch_size = batch_size
        
        # 缓存目录
        if cache_dir is None:
            self.cache_dir = os.path.join(self.output_folder, '.cache')
        else:
            self.cache_dir = cache_dir
        os.makedirs(self.cache_dir, exist_ok=True)
        
        # SQLite 数据库用于索引
        self.db_path = os.path.join(self.cache_dir, 'index.db')
        self.conn = None
        self.cursor = None
        
        # 统计信息
        self.stats = {
            'total_entries': 0,
            'unique_entries': 0,
            'duplicates_found': 0,
            'files_processed': 0,
            'batches_processed': 0
        }
        
    def _init_database(self):
        """初始化数据库"""
        self.conn = sqlite3.connect(self.db_path)
        self.cursor = self.conn.cursor()
        
        # 创建表：存储条目索引
        self.cursor.execute('''
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                doi TEXT NOT NULL,
                entry_idx INTEGER NOT NULL,
                material_name TEXT,
                composition_hash TEXT NOT NULL,
                tensile_value REAL,
                yield_value REAL,
                process_desc_hash TEXT,
                is_duplicate INTEGER DEFAULT 0,
                duplicate_of_id INTEGER,
                duplicate_reason TEXT,
                UNIQUE(doi, entry_idx)
            )
        ''')
        
        # 创建索引加速查询
        self.cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_composition_hash 
            ON entries(composition_hash)
        ''')
        
        self.cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_duplicate 
            ON entries(is_duplicate)
        ''')
        
        self.conn.commit()
        
        print("✅ 数据库索引初始化完成")
    
    def _close_database(self):
        """关闭数据库"""
        if self.conn:
            self.conn.commit()
            self.conn.close()
    
    def parse_entry(self, entry_str: str) -> Dict:
        """解析条目字符串"""
        entry_dict = {}
        pattern = r'([^：，]+)：([^，]*(?:，(?![^：，]+：)[^，]*)*)'
        matches = re.findall(pattern, entry_str)
        
        for key, value in matches:
            entry_dict[key.strip()] = value.strip().rstrip('，')
            
        return entry_dict
    
    def calculate_composition_hash(self, entry_dict: Dict) -> str:
        """计算化学成分哈希"""
        elements = ['H', 'B', 'C', 'N', 'O', 'F', 'Na', 'Mg', 'Al', 'Si', 
                   'P', 'S', 'Cl', 'Ca', 'Ti', 'V', 'Cr', 'Mn', 'Fe', 'Co', 
                   'Ni', 'Cu', 'Zn', 'As', 'Y', 'Zr', 'Nb', 'Mo', 'Sn', 'Sb', 
                   'La', 'Ce', 'Ta', 'W', 'Pb', 'Bi']
        
        composition = []
        for elem in elements:
            key = f'{elem}元素含量' if f'{elem}元素含量' in entry_dict else elem
            value = str(entry_dict.get(key, '0')).strip().lower().replace(' ', '')
            composition.append(f"{elem}:{value}")
        
        composition_str = '|'.join(composition)
        return hashlib.md5(composition_str.encode('utf-8')).hexdigest()
    
    def calculate_text_hash(self, text: str) -> str:
        """计算文本哈希（用于快速比对）"""
        # 只保留主要内容，去除数字和标点
        text_clean = re.sub(r'[0-9\s\.,，。]+', '', text)
        return hashlib.md5(text_clean.encode('utf-8')).hexdigest()[:16]
    
    def load_files_batch(self) -> Iterator[List[Tuple[str, str, Dict]]]:
        """
        批量加载文件（生成器模式，节省内存）
        
        Yields:
            [(doi, entry_str, entry_dict), ...]
        """
        files = [f for f in os.listdir(self.input_folder) if f.endswith('.json')]
        
        batch = []
        
        for filename in files:
            filepath = os.path.join(self.input_folder, filename)
            doi = filename.replace('.json', '')
            
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    entries = json.load(f)
                
                if not isinstance(entries, list):
                    continue
                
                for entry_str in entries:
                    if isinstance(entry_str, str) and len(entry_str) > 50:
                        entry_dict = self.parse_entry(entry_str)
                        batch.append((doi, entry_str, entry_dict))
                        
                        if len(batch) >= self.batch_size:
                            yield batch
                            batch = []
                            gc.collect()  # 释放内存
                
                self.stats['files_processed'] += 1
                
            except Exception as e:
                print(f"\n⚠️ 加载文件 {filename} 失败: {e}")
        
        # 返回最后一批
        if batch:
            yield batch
    
    def build_index(self):
        """
        构建索引（第一阶段）
        遍历所有条目，建立数据库索引
        """
        print("\n" + "="*70)
        print("📊 阶段 1/3: 构建索引")
        print("="*70)
        
        total_processed = 0
        
        for batch_idx, batch in enumerate(self.load_files_batch()):
            for doi, entry_str, entry_dict in batch:
                # 提取关键信息
                material_name = entry_dict.get('材料名称', '')
                comp_hash = self.calculate_composition_hash(entry_dict)
                
                tensile_value = entry_dict.get('张力值', '0')
                try:
                    tensile_value = float(tensile_value)
                except:
                    tensile_value = 0.0
                
                yield_value = entry_dict.get('屈服值', '0')
                try:
                    yield_value = float(yield_value)
                except:
                    yield_value = 0.0
                
                process_desc = entry_dict.get('ProcessDescription', '')
                process_hash = self.calculate_text_hash(process_desc)
                
                # 插入数据库
                self.cursor.execute('''
                    INSERT INTO entries 
                    (doi, entry_idx, material_name, composition_hash, 
                     tensile_value, yield_value, process_desc_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (doi, total_processed % self.batch_size, material_name, 
                      comp_hash, tensile_value, yield_value, process_hash))
                
                total_processed += 1
                self.stats['total_entries'] += 1
            
            self.stats['batches_processed'] += 1
            
            # 定期提交
            if batch_idx % 10 == 0:
                self.conn.commit()
                print(f"  已处理: {total_processed} 个条目，{self.stats['files_processed']} 个文件")
        
        self.conn.commit()
        
        print(f"\n✅ 索引构建完成，共 {total_processed} 个条目")
    
    def detect_duplicates(self):
        """
        检测重复（第二阶段）
        使用数据库查询优化比对过程
        """
        print("\n" + "="*70)
        print("🔍 阶段 2/3: 检测重复")
        print("="*70)
        
        # 查询所有不同的化学成分
        self.cursor.execute('''
            SELECT composition_hash, COUNT(*) as cnt 
            FROM entries 
            WHERE is_duplicate = 0
            GROUP BY composition_hash
            HAVING cnt > 1
        ''')
        
        comp_groups = self.cursor.fetchall()
        
        print(f"找到 {len(comp_groups)} 组可能重复的化学成分")
        
        duplicates_found = 0
        
        for comp_hash, count in tqdm(comp_groups, desc="检测重复"):
            # 获取该化学成分的所有条目
            self.cursor.execute('''
                SELECT id, doi, entry_idx, material_name, process_desc_hash
                FROM entries
                WHERE composition_hash = ? AND is_duplicate = 0
                ORDER BY id
            ''', (comp_hash,))
            
            entries = self.cursor.fetchall()
            
            if len(entries) < 2:
                continue
            
            # 两两比对
            for i in range(len(entries)):
                if entries[i] is None:
                    continue
                
                id1, doi1, idx1, name1, process_hash1 = entries[i]
                
                for j in range(i + 1, len(entries)):
                    if entries[j] is None:
                        continue
                    
                    id2, doi2, idx2, name2, process_hash2 = entries[j]
                    
                    # 跳过同一个 DOI
                    if doi1 == doi2:
                        continue
                    
                    is_dup = False
                    reason = ""
                    
                    # 精确重复：材料名称相同
                    if name1.lower().replace(' ', '') == name2.lower().replace(' ', ''):
                        is_dup = True
                        reason = "精确重复：材料名称和化学成分相同"
                    
                    # 相似重复：工艺流程哈希相同（快速判断）
                    elif process_hash1 == process_hash2:
                        is_dup = True
                        reason = "相似重复：化学成分和工艺流程高度相似"
                    
                    if is_dup:
                        # 标记为重复
                        self.cursor.execute('''
                            UPDATE entries 
                            SET is_duplicate = 1, duplicate_of_id = ?, duplicate_reason = ?
                            WHERE id = ?
                        ''', (id1, reason, id2))
                        
                        duplicates_found += 1
                        entries[j] = None  # 标记已处理
            
            # 定期提交
            if duplicates_found % 100 == 0:
                self.conn.commit()
        
        self.conn.commit()
        self.stats['duplicates_found'] = duplicates_found
        
        print(f"\n✅ 检测完成，发现 {duplicates_found} 个重复条目")
    
    def export_results(self):
        """
        导出结果（第三阶段）
        生成去重后的数据和报告
        """
        print("\n" + "="*70)
        print("💾 阶段 3/3: 导出结果")
        print("="*70)
        
        # 创建输出文件夹
        data_folder = os.path.join(self.output_folder, 'deduplicated_data')
        os.makedirs(data_folder, exist_ok=True)
        
        # 查询所有唯一条目（按 DOI 分组）
        self.cursor.execute('''
            SELECT DISTINCT doi FROM entries ORDER BY doi
        ''')
        
        dois = [row[0] for row in self.cursor.fetchall()]
        
        duplicate_report = []
        
        # 按 DOI 处理
        for doi in tqdm(dois, desc="导出文件"):
            # 获取该 DOI 的所有非重复条目
            self.cursor.execute('''
                SELECT entry_idx FROM entries
                WHERE doi = ? AND is_duplicate = 0
                ORDER BY entry_idx
            ''', (doi,))
            
            kept_indices = set(row[0] for row in self.cursor.fetchall())
            
            # 获取该 DOI 的所有重复条目（用于生成报告）
            self.cursor.execute('''
                SELECT e1.entry_idx, e2.doi, e2.entry_idx, e1.duplicate_reason
                FROM entries e1
                JOIN entries e2 ON e1.duplicate_of_id = e2.id
                WHERE e1.doi = ? AND e1.is_duplicate = 1
            ''', (doi,))
            
            dup_info = self.cursor.fetchall()
            
            # 读取原始文件
            filepath = os.path.join(self.input_folder, f"{doi}.json")
            
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    entries = json.load(f)
                
                # 过滤保留的条目
                kept_entries = [entries[i] for i in range(len(entries)) if i in kept_indices]
                
                # 保存去重后的数据
                if kept_entries:
                    output_path = os.path.join(data_folder, f"{doi}.json")
                    with open(output_path, 'w', encoding='utf-8') as f:
                        json.dump(kept_entries, f, ensure_ascii=False, indent=4)
                
                # 生成重复报告
                for removed_idx, dup_doi, dup_idx, reason in dup_info:
                    if removed_idx < len(entries):
                        # 读取重复来源
                        dup_filepath = os.path.join(self.input_folder, f"{dup_doi}.json")
                        with open(dup_filepath, 'r', encoding='utf-8') as f:
                            dup_entries = json.load(f)
                        
                        if dup_idx < len(dup_entries):
                            duplicate_report.append({
                                'removed_from': doi,
                                'removed_entry': entries[removed_idx],
                                'duplicate_of': dup_doi,
                                'original_entry': dup_entries[dup_idx],
                                'reason': reason
                            })
                
            except Exception as e:
                print(f"\n⚠️ 处理文件 {doi} 失败: {e}")
        
        # 保存重复报告
        report_path = os.path.join(self.output_folder, 'duplicate_report.json')
        with open(report_path, 'w', encoding='utf-8') as f:
            json.dump(duplicate_report, f, ensure_ascii=False, indent=4)
        
        print(f"✅ 重复报告已保存: {report_path}")
        
        # 保存统计信息
        self.stats['unique_entries'] = self.stats['total_entries'] - self.stats['duplicates_found']
        
        stats_path = os.path.join(self.output_folder, 'deduplication_stats.json')
        with open(stats_path, 'w', encoding='utf-8') as f:
            json.dump(self.stats, f, ensure_ascii=False, indent=4)
        
        print(f"✅ 统计信息已保存: {stats_path}")
    
    def deduplicate(self):
        """执行完整的去重流程"""
        print("\n" + "="*70)
        print("🚀 大规模数据去重系统")
        print("="*70)
        print(f"输入: {self.input_folder}")
        print(f"输出: {self.output_folder}")
        print(f"批处理大小: {self.batch_size}")
        print(f"相似度阈值: {self.similarity_threshold}")
        print("="*70)
        
        try:
            # 初始化数据库
            self._init_database()
            
            # 阶段 1: 构建索引
            self.build_index()
            
            # 阶段 2: 检测重复
            self.detect_duplicates()
            
            # 阶段 3: 导出结果
            self.export_results()
            
            # 打印统计信息
            self._print_stats()
            
        finally:
            # 清理
            self._close_database()
        
        print("\n✅ 去重完成！")
    
    def _print_stats(self):
        """打印统计信息"""
        print("\n" + "="*70)
        print("📊 去重统计报告")
        print("="*70)
        print(f"处理文件数: {self.stats['files_processed']}")
        print(f"总条目数: {self.stats['total_entries']}")
        print(f"去重后条目数: {self.stats['unique_entries']}")
        print(f"重复条目数: {self.stats['duplicates_found']}")
        
        if self.stats['total_entries'] > 0:
            dedup_rate = (self.stats['duplicates_found'] / self.stats['total_entries']) * 100
            print(f"去重率: {dedup_rate:.2f}%")
        
        print("="*70)


def main():
    """主函数"""
    
    # ==================== 配置区 ====================
    
    INPUT_FOLDER = "E:\Code\datapull\extract_data_origin\extract_data_2026-01-07_18-34-38"
    OUTPUT_FOLDER = None
    SIMILARITY_THRESHOLD = 0.90
    BATCH_SIZE = 1000  # 根据内存调整（500-2000）
    
    # ===============================================
    
    deduplicator = LargeScaleMaterialDeduplicator(
        input_folder=INPUT_FOLDER,
        output_folder=OUTPUT_FOLDER,
        similarity_threshold=SIMILARITY_THRESHOLD,
        batch_size=BATCH_SIZE
    )
    
    deduplicator.deduplicate()


if __name__ == "__main__":
    main()

