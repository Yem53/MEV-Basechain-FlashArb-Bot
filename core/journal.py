#!/usr/bin/env python3
"""
交易日志模块 (Trade Journal)

功能：
- 将所有交易尝试记录到 CSV 文件
- 支持后续性能分析和审计
- 线程安全的文件追加操作

使用方法：
    journal = TradeJournal()
    journal.log_trade(
        token_symbol="BRETT",
        borrow_amount=1.5,
        direction="BaseSwap -> Aerodrome",
        expected_profit=0.0043,
        tx_hash="0x...",
        status="Success",
        gas_used=250000
    )
"""

import os
import csv
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from dataclasses import dataclass


# ============================================
# 配置
# ============================================

# 日志文件目录
LOGS_DIR = Path(__file__).resolve().parent.parent / "logs"

# CSV 文件名
TRADE_HISTORY_FILE = "trade_history.csv"

# CSV 表头
CSV_HEADERS = [
    "Timestamp",
    "Token_Symbol", 
    "Borrow_Amount_ETH",
    "Direction",
    "Expected_Profit_ETH",
    "Tx_Hash",
    "Status",
    "Gas_Used",
    "Actual_Profit_ETH",
    "Notes"
]


# ============================================
# 数据结构
# ============================================

@dataclass
class TradeRecord:
    """交易记录"""
    timestamp: str
    token_symbol: str
    borrow_amount_eth: float
    direction: str
    expected_profit_eth: float
    tx_hash: str
    status: str
    gas_used: int = 0
    actual_profit_eth: float = 0.0
    notes: str = ""
    
    def to_row(self) -> list:
        """转换为 CSV 行"""
        return [
            self.timestamp,
            self.token_symbol,
            f"{self.borrow_amount_eth:.6f}",
            self.direction,
            f"{self.expected_profit_eth:.6f}",
            self.tx_hash,
            self.status,
            str(self.gas_used) if self.gas_used else "",
            f"{self.actual_profit_eth:.6f}" if self.actual_profit_eth else "",
            self.notes
        ]


# ============================================
# TradeJournal 类
# ============================================

class TradeJournal:
    """
    交易日志管理器
    
    将所有交易尝试记录到 CSV 文件，用于后续分析和审计。
    """
    
    def __init__(self, log_dir: Optional[Path] = None):
        """
        初始化交易日志
        
        参数：
            log_dir: 日志目录路径（默认为项目根目录下的 logs/）
        """
        self.log_dir = log_dir or LOGS_DIR
        self.file_path = self.log_dir / TRADE_HISTORY_FILE
        self._lock = threading.Lock()
        
        # 确保目录存在
        self._ensure_directory()
        
        # 确保文件存在并有表头
        self._ensure_file()
    
    def _ensure_directory(self):
        """确保日志目录存在"""
        if not self.log_dir.exists():
            self.log_dir.mkdir(parents=True, exist_ok=True)
            print(f"📁 创建日志目录: {self.log_dir}")
    
    def _ensure_file(self):
        """确保 CSV 文件存在并有正确的表头"""
        if not self.file_path.exists():
            with open(self.file_path, 'w', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(CSV_HEADERS)
            print(f"📄 创建交易日志: {self.file_path}")
        else:
            # 检查文件是否为空
            if self.file_path.stat().st_size == 0:
                with open(self.file_path, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerow(CSV_HEADERS)
    
    def log_trade(
        self,
        token_symbol: str,
        borrow_amount: float,
        direction: str,
        expected_profit: float,
        tx_hash: str,
        status: str = "Pending",
        gas_used: int = 0,
        actual_profit: float = 0.0,
        notes: str = ""
    ) -> TradeRecord:
        """
        记录一笔交易
        
        参数：
            token_symbol: 代币符号 (如 "BRETT")
            borrow_amount: 借入金额 (ETH)
            direction: 交易方向 (如 "BaseSwap -> Aerodrome")
            expected_profit: 预期利润 (ETH)
            tx_hash: 交易哈希
            status: 交易状态 ("Pending", "Success", "Revert", "Failed")
            gas_used: 使用的 Gas
            actual_profit: 实际利润 (ETH)
            notes: 备注
            
        返回：
            TradeRecord 记录对象
        """
        # 创建记录
        record = TradeRecord(
            timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
            token_symbol=token_symbol,
            borrow_amount_eth=borrow_amount,
            direction=direction,
            expected_profit_eth=expected_profit,
            tx_hash=tx_hash,
            status=status,
            gas_used=gas_used,
            actual_profit_eth=actual_profit,
            notes=notes
        )
        
        # 线程安全写入
        with self._lock:
            with open(self.file_path, 'a', newline='', encoding='utf-8') as f:
                writer = csv.writer(f)
                writer.writerow(record.to_row())
        
        return record
    
    def log_opportunity(
        self,
        token_symbol: str,
        borrow_amount: float,
        direction: str,
        expected_profit: float,
        notes: str = "Opportunity detected, not executed"
    ) -> TradeRecord:
        """
        记录一个发现的机会（未执行）
        
        用于 Dry Run 模式或仅记录机会
        """
        return self.log_trade(
            token_symbol=token_symbol,
            borrow_amount=borrow_amount,
            direction=direction,
            expected_profit=expected_profit,
            tx_hash="N/A",
            status="DryRun",
            notes=notes
        )
    
    def update_status(
        self,
        tx_hash: str,
        status: str,
        gas_used: int = 0,
        actual_profit: float = 0.0
    ) -> bool:
        """
        更新交易状态（通过 tx_hash 查找）
        
        注意：这是一个简化实现，会读取整个文件并重写。
        对于高频交易，建议使用数据库。
        
        参数：
            tx_hash: 交易哈希
            status: 新状态
            gas_used: 使用的 Gas
            actual_profit: 实际利润
            
        返回：
            是否成功更新
        """
        with self._lock:
            # 读取所有行
            rows = []
            updated = False
            
            with open(self.file_path, 'r', newline='', encoding='utf-8') as f:
                reader = csv.reader(f)
                headers = next(reader)
                rows.append(headers)
                
                for row in reader:
                    if len(row) >= 6 and row[5] == tx_hash:
                        # 更新状态
                        row[6] = status
                        if gas_used:
                            row[7] = str(gas_used)
                        if actual_profit:
                            row[8] = f"{actual_profit:.6f}"
                        updated = True
                    rows.append(row)
            
            if updated:
                # 重写文件
                with open(self.file_path, 'w', newline='', encoding='utf-8') as f:
                    writer = csv.writer(f)
                    writer.writerows(rows)
            
            return updated
    
    def get_stats(self) -> dict:
        """
        获取交易统计信息
        
        返回：
            统计字典
        """
        stats = {
            "total_trades": 0,
            "successful": 0,
            "reverted": 0,
            "pending": 0,
            "dry_run": 0,
            "total_profit_eth": 0.0,
            "total_gas_used": 0
        }
        
        with self._lock:
            with open(self.file_path, 'r', newline='', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                
                for row in reader:
                    stats["total_trades"] += 1
                    status = row.get("Status", "").lower()
                    
                    if status == "success":
                        stats["successful"] += 1
                        try:
                            stats["total_profit_eth"] += float(row.get("Actual_Profit_ETH") or 0)
                        except ValueError:
                            pass
                    elif status == "revert":
                        stats["reverted"] += 1
                    elif status == "pending":
                        stats["pending"] += 1
                    elif status == "dryrun":
                        stats["dry_run"] += 1
                    
                    try:
                        stats["total_gas_used"] += int(row.get("Gas_Used") or 0)
                    except ValueError:
                        pass
        
        return stats
    
    def print_summary(self):
        """打印交易摘要"""
        stats = self.get_stats()
        
        print("\n" + "=" * 50)
        print("📊 Trade Journal Summary")
        print("=" * 50)
        print(f"  Total Trades:    {stats['total_trades']}")
        print(f"  Successful:      {stats['successful']}")
        print(f"  Reverted:        {stats['reverted']}")
        print(f"  Pending:         {stats['pending']}")
        print(f"  Dry Run:         {stats['dry_run']}")
        print(f"  Total Profit:    {stats['total_profit_eth']:.6f} ETH")
        print(f"  Total Gas Used:  {stats['total_gas_used']:,}")
        print(f"  Log File:        {self.file_path}")
        print("=" * 50 + "\n")


# ============================================
# 便捷函数
# ============================================

# 全局日志实例
_global_journal: Optional[TradeJournal] = None


def get_journal() -> TradeJournal:
    """获取全局日志实例"""
    global _global_journal
    if _global_journal is None:
        _global_journal = TradeJournal()
    return _global_journal


def log_trade(**kwargs) -> TradeRecord:
    """快捷日志函数"""
    return get_journal().log_trade(**kwargs)


# ============================================
# 测试
# ============================================

if __name__ == "__main__":
    # 测试日志功能
    print("测试 TradeJournal...")
    
    journal = TradeJournal()
    
    # 记录测试交易
    record = journal.log_trade(
        token_symbol="BRETT",
        borrow_amount=1.5,
        direction="BaseSwap -> Aerodrome",
        expected_profit=0.0043,
        tx_hash="0x1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
        status="Success",
        gas_used=250000,
        actual_profit=0.0041
    )
    
    print(f"记录已保存: {record}")
    
    # 记录 Dry Run
    journal.log_opportunity(
        token_symbol="TOSHI",
        borrow_amount=2.0,
        direction="SushiSwap -> BaseSwap",
        expected_profit=0.0021
    )
    
    # 打印摘要
    journal.print_summary()
    
    print(f"✅ 日志文件: {journal.file_path}")

