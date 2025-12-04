#!/usr/bin/env python3
"""
套利执行器模块

功能：
- 构建、签名和发送套利交易
- 管理 Nonce 避免冲突
- 支持 EIP-1559 和 Legacy 两种 Gas 模式
- 编码 userData 参数

使用示例：
    executor = ArbitrageExecutor(w3, contract, private_key)
    tx_hash = executor.execute_trade(
        direction="forward",
        borrow_amount=10**18,
        pair_address=pair_a,
        target_router=router_b,
        trade_path=[weth, usdc, weth]
    )
"""

import os
import time
import threading
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass
from enum import Enum

from web3 import Web3
from web3.contract import Contract
from eth_abi import encode
from eth_account import Account


# ============================================
# 常量定义
# ============================================

# Gas 限制（闪电贷套利交易）
DEFAULT_GAS_LIMIT = 500000

# Gas 价格上限（Gwei）- 防止意外高 Gas
MAX_GAS_PRICE_GWEI = 1.0  # Base 上通常很低

# 交易确认超时（秒）
TX_TIMEOUT = 60

# Nonce 重试次数
MAX_NONCE_RETRIES = 3

# 最小 Gas 消耗阈值（用于检测软失败）
# 真正的套利交易通常消耗 >100k gas
# 软失败（early exit）通常只消耗 ~30k gas
MIN_GAS_FOR_SUCCESS = 80000

# Aerodrome Router 地址 (Base Mainnet)
AERODROME_ROUTER = "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"

# Aerodrome Factory 地址 (Base Mainnet)
AERODROME_FACTORY = "0x420DD381b31aEf6683db6B902084cB0FFECe40Da"


class TradeDirection(Enum):
    """交易方向枚举"""
    FORWARD = "forward"   # Pair A -> Pair B
    REVERSE = "reverse"   # Pair B -> Pair A


@dataclass
class ExecutionResult:
    """执行结果"""
    success: bool
    tx_hash: Optional[str] = None
    gas_used: int = 0
    gas_price: int = 0
    error: Optional[str] = None
    profit_realized: int = 0
    # 性能统计（毫秒）
    time_simulation_ms: float = 0.0
    time_signing_ms: float = 0.0
    time_broadcast_ms: float = 0.0
    time_confirmation_ms: float = 0.0
    time_total_ms: float = 0.0


@dataclass
class TradeParams:
    """交易参数"""
    direction: str
    borrow_amount: int
    pair_address: str
    target_router: str
    trade_path: List[str]
    token_borrow: str
    expected_profit: int = 0


# ============================================
# 套利执行器类
# ============================================

class ArbitrageExecutor:
    """
    套利执行器
    
    负责构建、签名和发送套利交易到 FlashBot 合约。
    """
    
    def __init__(
        self,
        w3: Web3,
        contract: Contract,
        private_key: str,
        gas_limit: int = DEFAULT_GAS_LIMIT,
        max_gas_price_gwei: float = MAX_GAS_PRICE_GWEI
    ):
        """
        初始化执行器
        
        参数：
            w3: Web3 实例
            contract: FlashBot 合约实例
            private_key: 私钥（用于签名交易）
            gas_limit: Gas 限制
            max_gas_price_gwei: 最大 Gas 价格（Gwei）
        """
        self.w3 = w3
        self.contract = contract
        self.gas_limit = gas_limit
        self.max_gas_price_gwei = max_gas_price_gwei
        
        # 加载账户
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        self.account = Account.from_key(private_key)
        self.address = self.account.address
        
        # Nonce 管理（线程安全）
        self._nonce_lock = threading.Lock()
        self._pending_nonce: Optional[int] = None
        self._last_nonce_fetch = 0
        
        # 统计信息
        self.tx_count = 0
        self.success_count = 0
        self.failed_count = 0
        self.total_profit = 0
    
    def _get_nonce(self) -> int:
        """
        获取下一个可用的 nonce
        
        使用本地管理 + pending 获取，避免 nonce 冲突
        """
        with self._nonce_lock:
            current_time = time.time()
            
            # 每 5 秒或首次获取时，从链上获取 pending nonce
            if self._pending_nonce is None or current_time - self._last_nonce_fetch > 5:
                self._pending_nonce = self.w3.eth.get_transaction_count(
                    self.address, 
                    "pending"
                )
                self._last_nonce_fetch = current_time
            
            nonce = self._pending_nonce
            self._pending_nonce += 1
            
            return nonce
    
    def _reset_nonce(self):
        """重置 nonce 缓存"""
        with self._nonce_lock:
            self._pending_nonce = None
            self._last_nonce_fetch = 0
    
    def _get_gas_params(self, sniper_mode: bool = True) -> Dict[str, int]:
        """
        获取 Gas 参数 (支持 Sniper Mode)
        
        自动检测是否支持 EIP-1559
        
        参数：
            sniper_mode: 是否启用 Sniper Mode（增加 20% 优先费以确保快速打包）
        """
        try:
            # 尝试获取最新区块的 baseFee（EIP-1559）
            latest_block = self.w3.eth.get_block("latest")
            base_fee = latest_block.get("baseFeePerGas")
            
            if base_fee is not None:
                # EIP-1559 模式
                # 🎯 Sniper Mode: 从链上获取真实的 maxPriorityFeePerGas 并提升 20%
                try:
                    # 获取网络建议的优先费
                    suggested_priority_fee = self.w3.eth.max_priority_fee
                    
                    if sniper_mode:
                        # Sniper Mode: 增加 20% tip boost 以抢占区块位置
                        priority_fee = int(suggested_priority_fee * 1.2)
                    else:
                        priority_fee = suggested_priority_fee
                    
                    # 确保优先费至少为 0.01 Gwei
                    min_priority_fee = self.w3.to_wei(0.01, "gwei")
                    priority_fee = max(priority_fee, min_priority_fee)
                    
                except Exception:
                    # 如果获取失败，使用默认值
                    priority_fee = self.w3.to_wei(0.01, "gwei")
                    if sniper_mode:
                        priority_fee = int(priority_fee * 1.2)
                
                # maxFeePerGas: base_fee * 2 + priority_fee
                max_fee = base_fee * 2 + priority_fee
                
                # 检查上限
                max_allowed = self.w3.to_wei(self.max_gas_price_gwei, "gwei")
                if max_fee > max_allowed:
                    max_fee = max_allowed
                
                return {
                    "maxFeePerGas": max_fee,
                    "maxPriorityFeePerGas": priority_fee,
                }
            else:
                # Legacy 模式
                gas_price = self.w3.eth.gas_price
                
                if sniper_mode:
                    # Sniper Mode: 增加 20% gas price
                    gas_price = int(gas_price * 1.2)
                
                max_allowed = self.w3.to_wei(self.max_gas_price_gwei, "gwei")
                
                if gas_price > max_allowed:
                    gas_price = max_allowed
                
                return {"gasPrice": gas_price}
                
        except Exception:
            # 回退到 Legacy 模式
            gas_price = self.w3.to_wei(0.01, "gwei")
            if sniper_mode:
                gas_price = int(gas_price * 1.2)
            return {"gasPrice": gas_price}
    
    def _encode_user_data(
        self,
        target_router: str,
        trade_path: List[str]
    ) -> bytes:
        """
        编码 userData 参数（单路由器模式 - 向后兼容）
        
        格式：abi.encode(address, address[])
        
        参数：
            target_router: 目标路由器地址
            trade_path: 交易路径
            
        返回：
            编码后的 bytes
        """
        # 确保地址是校验和格式
        target_router = self.w3.to_checksum_address(target_router)
        trade_path = [self.w3.to_checksum_address(addr) for addr in trade_path]
        
        # 编码
        encoded = encode(
            ["address", "address[]"],
            [target_router, trade_path]
        )
        
        return encoded
    
    def _encode_cross_dex_data(
        self,
        router1: str,
        path1: List[str],
        router2: str,
        path2: List[str]
    ) -> bytes:
        """
        编码跨 DEX 套利的 userData 参数（V2 模式）
        
        格式：abi.encode(address, address[], address, address[])
        
        参数：
            router1: 第一跳路由器（执行 借入代币 -> 中间代币）
            path1: 第一跳路径 [借入代币, 中间代币]
            router2: 第二跳路由器（执行 中间代币 -> 借入代币）
            path2: 第二跳路径 [中间代币, 借入代币]
            
        返回：
            编码后的 bytes
        """
        # 确保地址是校验和格式
        router1 = self.w3.to_checksum_address(router1)
        router2 = self.w3.to_checksum_address(router2)
        path1 = [self.w3.to_checksum_address(addr) for addr in path1]
        path2 = [self.w3.to_checksum_address(addr) for addr in path2]
        
        # 编码
        encoded = encode(
            ["address", "address[]", "address", "address[]"],
            [router1, path1, router2, path2]
        )
        
        return encoded
    
    def _encode_hybrid_data(
        self,
        router1: str,
        path1: List[str],
        router2: str,
        routes: List[Tuple[str, str, bool, str]]
    ) -> bytes:
        """
        编码混合模式 userData 参数（V2 + Solidly）
        
        格式：abi.encode(address, address[], address, (address,address,bool,address)[])
        
        参数：
            router1: 第一跳路由器（V2 或 Solidly）
            path1: 第一跳路径 [借入代币, 中间代币]
            router2: 第二跳路由器（Solidly Router - Aerodrome）
            routes: Solidly Route 列表 [(from, to, stable, factory), ...]
            
        返回：
            编码后的 bytes
        """
        # 确保地址是校验和格式
        router1 = self.w3.to_checksum_address(router1)
        router2 = self.w3.to_checksum_address(router2)
        path1 = [self.w3.to_checksum_address(addr) for addr in path1]
        
        # 处理 routes - 包含 factory 地址
        routes_formatted = [
            (
                self.w3.to_checksum_address(r[0]),  # from
                self.w3.to_checksum_address(r[1]),  # to
                r[2],                                # stable
                self.w3.to_checksum_address(r[3])   # factory
            )
            for r in routes
        ]
        
        # 编码
        # Route 结构体: (address from, address to, bool stable, address factory)
        encoded = encode(
            ["address", "address[]", "address", "(address,address,bool,address)[]"],
            [router1, path1, router2, routes_formatted]
        )
        
        return encoded
    
    def _path_to_routes(
        self, 
        path: List[str], 
        stable: bool = False,
        factory: str = AERODROME_FACTORY
    ) -> List[Tuple[str, str, bool, str]]:
        """
        将地址路径转换为 Solidly Route 列表
        
        参数：
            path: 地址路径 [tokenA, tokenB, tokenC]
            stable: 是否使用稳定池
            factory: 工厂地址（Aerodrome 必需）
            
        返回：
            Route 列表 [(from, to, stable, factory), ...]
        """
        routes = []
        for i in range(len(path) - 1):
            routes.append((path[i], path[i + 1], stable, factory))
        return routes
    
    def _is_aerodrome(self, router: str) -> bool:
        """检查路由器是否是 Aerodrome"""
        return self.w3.to_checksum_address(router).lower() == AERODROME_ROUTER.lower()
    
    def execute_trade(
        self,
        direction: str,
        borrow_amount: int,
        pair_address: str,
        target_router: str,
        trade_path: List[str],
        token_borrow: str,
        expected_profit: int = 0,
        dry_run: bool = False,
        # 跨 DEX 参数（可选）
        router2: str = None,
        path2: List[str] = None
    ) -> ExecutionResult:
        """
        执行套利交易
        
        参数：
            direction: 交易方向 ("forward" 或 "reverse")
            borrow_amount: 借入金额（wei）
            pair_address: 闪电贷配对地址
            target_router: 目标路由器地址（第一跳 / 单路由器模式）
            trade_path: 交易路径（第一跳路径 / 单路由器完整路径）
            token_borrow: 借入代币地址
            expected_profit: 预期利润（用于日志）
            dry_run: 是否只模拟不实际发送
            router2: 第二跳路由器（跨 DEX 模式）
            path2: 第二跳路径（跨 DEX 模式）
            
        返回：
            ExecutionResult 结果对象
        """
        start_time = time.time()
        
        try:
            # 1. 验证参数
            pair_address = self.w3.to_checksum_address(pair_address)
            token_borrow = self.w3.to_checksum_address(token_borrow)
            
            # 2. 编码 userData
            # 简化版：始终使用 (router1, path1, router2, path2) 格式
            # 合约会自动检测 Aerodrome 并转换为 Solidly 调用
            if router2 and path2:
                # 跨 DEX 模式（统一格式）
                user_data = self._encode_cross_dex_data(
                    target_router, trade_path,
                    router2, path2
                )
            else:
                # 单路由器模式（向后兼容）
                user_data = self._encode_user_data(target_router, trade_path)
            
            # 3. 获取 Gas 参数
            gas_params = self._get_gas_params()
            
            # 4. 获取 nonce
            nonce = self._get_nonce()
            
            # 5. 构建交易
            tx = self.contract.functions.startArbitrage(
                token_borrow,
                borrow_amount,
                pair_address,
                user_data
            ).build_transaction({
                "from": self.address,
                "nonce": nonce,
                "gas": self.gas_limit,
                **gas_params
            })
            
            if dry_run:
                # 模拟模式：只返回交易数据
                return ExecutionResult(
                    success=True,
                    tx_hash=None,
                    gas_used=0,
                    gas_price=gas_params.get("gasPrice", gas_params.get("maxFeePerGas", 0)),
                    error="Dry run - 未实际发送"
                )
            
            # 6. 预执行模拟（Pre-Execution Simulation）
            # 使用 eth_call 模拟交易，避免发送会失败的交易
            t_sim_start = time.time()
            try:
                # 构建调用参数（移除 nonce，call 不需要）
                call_params = {
                    "from": self.address,
                    "gas": self.gas_limit,
                    **gas_params
                }
                
                # 模拟交易执行
                self.contract.functions.startArbitrage(
                    token_borrow,
                    borrow_amount,
                    pair_address,
                    user_data
                ).call(call_params)
                
                # 模拟成功，继续执行
                
            except Exception as sim_error:
                # 模拟失败（交易会 revert）
                # 注意：不增加 tx_count，因为交易未实际发送，节省了 gas
                # 重置 nonce 缓存，因为交易未发送，nonce 未被消耗
                self._reset_nonce()
                error_msg = str(sim_error)
                t_sim_end = time.time()
                time_sim_ms = (t_sim_end - t_sim_start) * 1000
                
                return ExecutionResult(
                    success=False,
                    tx_hash=None,
                    gas_used=0,
                    gas_price=gas_params.get("gasPrice", gas_params.get("maxFeePerGas", 0)),
                    error=f"Simulation failed: {error_msg}",
                    profit_realized=0,
                    time_simulation_ms=time_sim_ms,
                    time_total_ms=(t_sim_end - start_time) * 1000
                )
            t_sim_end = time.time()
            time_sim_ms = (t_sim_end - t_sim_start) * 1000
            
            # 7. 签名交易
            t_sign_start = time.time()
            signed_tx = self.account.sign_transaction(tx)
            t_sign_end = time.time()
            time_sign_ms = (t_sign_end - t_sign_start) * 1000
            
            # --- 8. 鲁棒地获取原始交易字节 (兼容不同 Web3.py 版本) ---
            raw_tx_bytes = None
            
            # 尝试 camelCase (Web3.py v5 和大多数 v6)
            if hasattr(signed_tx, 'rawTransaction'):
                raw_tx_bytes = signed_tx.rawTransaction
            # 尝试 snake_case (某些特定 v6 版本)
            elif hasattr(signed_tx, 'raw_transaction'):
                raw_tx_bytes = signed_tx.raw_transaction
            # 回退：字典访问
            elif isinstance(signed_tx, dict) and 'rawTransaction' in signed_tx:
                raw_tx_bytes = signed_tx['rawTransaction']
            elif isinstance(signed_tx, dict) and 'raw_transaction' in signed_tx:
                raw_tx_bytes = signed_tx['raw_transaction']
            
            if raw_tx_bytes is None:
                # 最后尝试：检查对象的所有属性
                for attr in dir(signed_tx):
                    if 'raw' in attr.lower() and 'transaction' in attr.lower():
                        raw_tx_bytes = getattr(signed_tx, attr, None)
                        if raw_tx_bytes:
                            break
            
            if raw_tx_bytes is None:
                self._reset_nonce()
                return ExecutionResult(
                    success=False,
                    tx_hash=None,
                    gas_used=0,
                    gas_price=gas_params.get("gasPrice", gas_params.get("maxFeePerGas", 0)),
                    error=f"Could not extract raw bytes from SignedTransaction. Available attrs: {[a for a in dir(signed_tx) if not a.startswith('_')]}",
                    profit_realized=0,
                    time_simulation_ms=time_sim_ms,
                    time_signing_ms=time_sign_ms,
                    time_total_ms=(time.time() - start_time) * 1000
                )
            
            # 9. 发送交易 (使用安全提取的原始字节)
            t_broadcast_start = time.time()
            tx_hash = self.w3.eth.send_raw_transaction(raw_tx_bytes)
            tx_hash_hex = tx_hash.hex()
            t_broadcast_end = time.time()
            time_broadcast_ms = (t_broadcast_end - t_broadcast_start) * 1000
            
            # 10. 等待确认
            t_confirm_start = time.time()
            receipt = self.w3.eth.wait_for_transaction_receipt(
                tx_hash, 
                timeout=TX_TIMEOUT
            )
            t_confirm_end = time.time()
            time_confirm_ms = (t_confirm_end - t_confirm_start) * 1000
            
            # 11. 检查结果
            tx_status = receipt["status"] == 1
            gas_used = receipt["gasUsed"]
            
            # 检测软失败：交易状态成功但 gas 消耗过低
            # 真正的套利交易消耗 >100k gas，软失败只消耗 ~30k
            is_soft_fail = tx_status and gas_used < MIN_GAS_FOR_SUCCESS
            
            if is_soft_fail:
                # 软失败：合约内部提前退出，没有执行真正的套利
                success = False
                error_msg = f"Soft fail detected: only {gas_used} gas used (expected >{MIN_GAS_FOR_SUCCESS})"
            else:
                success = tx_status
                error_msg = None if success else "Transaction reverted"
            
            # 更新统计
            self.tx_count += 1
            if success:
                self.success_count += 1
                self.total_profit += expected_profit
            else:
                self.failed_count += 1
            
            # 计算总耗时
            time_total_ms = (time.time() - start_time) * 1000
            
            return ExecutionResult(
                success=success,
                tx_hash=tx_hash_hex,
                gas_used=gas_used,
                gas_price=gas_params.get("gasPrice", gas_params.get("maxFeePerGas", 0)),
                profit_realized=expected_profit if success else 0,
                error=error_msg,
                time_simulation_ms=time_sim_ms,
                time_signing_ms=time_sign_ms,
                time_broadcast_ms=time_broadcast_ms,
                time_confirmation_ms=time_confirm_ms,
                time_total_ms=time_total_ms
            )
            
        except Exception as e:
            # 重置 nonce 缓存以防出错
            self._reset_nonce()
            self.failed_count += 1
            self.tx_count += 1
            
            return ExecutionResult(
                success=False,
                tx_hash=None,
                gas_used=0,
                gas_price=0,
                error=str(e)
            )
    
    def estimate_gas(
        self,
        borrow_amount: int,
        pair_address: str,
        target_router: str,
        trade_path: List[str],
        token_borrow: str
    ) -> Optional[int]:
        """
        估算交易 Gas 消耗
        
        返回：
            Gas 估算值或 None（如果估算失败）
        """
        try:
            pair_address = self.w3.to_checksum_address(pair_address)
            token_borrow = self.w3.to_checksum_address(token_borrow)
            user_data = self._encode_user_data(target_router, trade_path)
            
            gas_estimate = self.contract.functions.startArbitrage(
                token_borrow,
                borrow_amount,
                pair_address,
                user_data
            ).estimate_gas({"from": self.address})
            
            return gas_estimate
            
        except Exception:
            return None
    
    def get_balance(self) -> int:
        """获取账户 ETH 余额"""
        return self.w3.eth.get_balance(self.address)
    
    def get_stats(self) -> Dict[str, Any]:
        """获取执行统计信息"""
        return {
            "address": self.address,
            "tx_count": self.tx_count,
            "success_count": self.success_count,
            "failed_count": self.failed_count,
            "success_rate": self.success_count / self.tx_count if self.tx_count > 0 else 0,
            "total_profit_wei": self.total_profit,
            "total_profit_eth": self.total_profit / 10**18,
        }


# ============================================
# 辅助函数
# ============================================

def create_executor_from_env(
    w3: Web3,
    contract: Contract
) -> ArbitrageExecutor:
    """
    从环境变量创建执行器
    
    需要的环境变量：
    - PRIVATE_KEY: 私钥
    """
    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        raise ValueError("未设置 PRIVATE_KEY 环境变量")
    
    return ArbitrageExecutor(w3, contract, private_key)


# ============================================
# 测试代码
# ============================================

if __name__ == "__main__":
    import json
    from pathlib import Path
    from dotenv import load_dotenv
    
    # 加载环境变量
    PROJECT_ROOT = Path(__file__).parent.parent
    load_dotenv(PROJECT_ROOT / ".env")
    
    # 连接到网络
    rpc_url = os.getenv("RPC_URL", "http://127.0.0.1:8545")
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    
    if not w3.is_connected():
        print("无法连接到网络")
        exit(1)
    
    print(f"已连接，链 ID: {w3.eth.chain_id}")
    
    # 加载合约
    deployments_file = PROJECT_ROOT / "deployments.json"
    if not deployments_file.exists():
        print("未找到部署文件")
        exit(1)
    
    deployments = json.loads(deployments_file.read_text())
    chain_id = str(w3.eth.chain_id)
    
    if chain_id not in deployments:
        print(f"未找到链 {chain_id} 的部署信息")
        exit(1)
    
    contract_address = w3.to_checksum_address(deployments[chain_id]["contract_address"])
    abi = deployments[chain_id]["abi"]
    contract = w3.eth.contract(address=contract_address, abi=abi)
    
    print(f"FlashBot 合约: {contract_address}")
    
    # 创建执行器
    executor = create_executor_from_env(w3, contract)
    print(f"执行器地址: {executor.address}")
    print(f"账户余额: {executor.get_balance() / 10**18:.4f} ETH")
    
    # 测试 Gas 参数获取
    gas_params = executor._get_gas_params()
    print(f"Gas 参数: {gas_params}")
    
    # 测试 userData 编码
    test_router = "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24"
    test_path = [
        "0x4200000000000000000000000000000000000006",
        "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA",
    ]
    user_data = executor._encode_user_data(test_router, test_path)
    print(f"userData 长度: {len(user_data)} bytes")
    
    print("\n执行器测试完成!")

