#!/usr/bin/env python3
"""
Uniswap V3 原生闪电贷执行器

功能：
- 调用 FlashBotV3 合约的 startArbitrage 函数
- 编码 V3 闪电贷和交换参数
- 支持 V3-V3、V3-V2、V3-Solidly 跨协议套利

调用流程：
1. Python 调用合约的 startArbitrage(poolAddress, tokenBorrow, amount, userData)
2. 合约调用 V3 池的 flash()
3. 池回调合约的 uniswapV3FlashCallback()
4. 合约执行套利交换并偿还
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
# 常量
# ============================================

# V3 常量 - Base Mainnet
V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
V3_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
WETH_ADDRESS = "0x4200000000000000000000000000000000000006"

# Gas 配置
DEFAULT_GAS_LIMIT = 600000  # V3 闪电贷需要更多 gas
MAX_GAS_PRICE_GWEI = 1.0
TX_TIMEOUT = 60

# 交换类型
class SwapType(Enum):
    V3 = 0
    V2 = 1
    SOLIDLY = 2
    CROSS_PROTOCOL = 3


@dataclass
class V3ExecutionResult:
    """V3 执行结果"""
    success: bool
    tx_hash: Optional[str] = None
    gas_used: int = 0
    gas_price: int = 0
    error: Optional[str] = None
    profit_realized: int = 0
    flash_fee_paid: int = 0
    # 性能指标
    time_simulation_ms: float = 0.0
    time_signing_ms: float = 0.0
    time_broadcast_ms: float = 0.0
    time_confirmation_ms: float = 0.0
    time_total_ms: float = 0.0


# ============================================
# V3 执行器类
# ============================================

class V3ArbitrageExecutor:
    """
    V3 原生闪电贷套利执行器
    
    调用 FlashBotV3 合约执行套利交易。
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
        初始化 V3 执行器
        
        参数：
            w3: Web3 实例
            contract: FlashBotV3 合约实例
            private_key: 私钥
            gas_limit: Gas 限制
            max_gas_price_gwei: 最大 Gas 价格
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
        
        # Nonce 管理
        self._nonce_lock = threading.Lock()
        self._pending_nonce: Optional[int] = None
        self._last_nonce_fetch = 0
        
        # 统计
        self.tx_count = 0
        self.success_count = 0
        self.failed_count = 0
        self.total_profit = 0
    
    def _get_nonce(self) -> int:
        """获取下一个可用的 nonce"""
        with self._nonce_lock:
            current_time = time.time()
            
            if self._pending_nonce is None or current_time - self._last_nonce_fetch > 5:
                self._pending_nonce = self.w3.eth.get_transaction_count(
                    self.address, "pending"
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
        获取 Gas 参数（支持 Sniper Mode）
        """
        try:
            latest_block = self.w3.eth.get_block("latest")
            base_fee = latest_block.get("baseFeePerGas")
            
            if base_fee is not None:
                try:
                    suggested_priority_fee = self.w3.eth.max_priority_fee
                    
                    if sniper_mode:
                        priority_fee = int(suggested_priority_fee * 1.2)
                    else:
                        priority_fee = suggested_priority_fee
                    
                    min_priority_fee = self.w3.to_wei(0.01, "gwei")
                    priority_fee = max(priority_fee, min_priority_fee)
                    
                except Exception:
                    priority_fee = self.w3.to_wei(0.01, "gwei")
                    if sniper_mode:
                        priority_fee = int(priority_fee * 1.2)
                
                max_fee = base_fee * 2 + priority_fee
                max_allowed = self.w3.to_wei(self.max_gas_price_gwei, "gwei")
                if max_fee > max_allowed:
                    max_fee = max_allowed
                
                return {
                    "maxFeePerGas": max_fee,
                    "maxPriorityFeePerGas": priority_fee,
                }
            else:
                gas_price = self.w3.eth.gas_price
                if sniper_mode:
                    gas_price = int(gas_price * 1.2)
                
                max_allowed = self.w3.to_wei(self.max_gas_price_gwei, "gwei")
                if gas_price > max_allowed:
                    gas_price = max_allowed
                
                return {"gasPrice": gas_price}
                
        except Exception:
            gas_price = self.w3.to_wei(0.01, "gwei")
            if sniper_mode:
                gas_price = int(gas_price * 1.2)
            return {"gasPrice": gas_price}
    
    def _encode_v3_swap_data(
        self,
        token_in: str,
        token_out: str,
        fee: int
    ) -> bytes:
        """
        编码 V3 交换数据
        
        参数：
            token_in: 输入代币
            token_out: 输出代币
            fee: 池费率
        
        返回：
            编码后的数据
        """
        return encode(
            ['address', 'address', 'uint24'],
            [
                self.w3.to_checksum_address(token_in),
                self.w3.to_checksum_address(token_out),
                fee
            ]
        )
    
    def _encode_v2_swap_data(
        self,
        router: str,
        path: List[str]
    ) -> bytes:
        """
        编码 V2 交换数据
        """
        return encode(
            ['address', 'address[]'],
            [
                self.w3.to_checksum_address(router),
                [self.w3.to_checksum_address(addr) for addr in path]
            ]
        )
    
    def _encode_solidly_swap_data(self, path: List[str]) -> bytes:
        """
        编码 Solidly 交换数据
        """
        return encode(
            ['address[]'],
            [[self.w3.to_checksum_address(addr) for addr in path]]
        )
    
    def _encode_user_data(
        self,
        swap_type: SwapType,
        swap_params: bytes
    ) -> bytes:
        """
        编码 userData（传递给合约回调）
        
        格式：abi.encode(uint8 swapType, bytes swapParams)
        """
        return encode(
            ['uint8', 'bytes'],
            [swap_type.value, swap_params]
        )
    
    def _encode_cross_protocol_data(
        self,
        swap_type1: SwapType,
        params1: bytes,
        swap_type2: SwapType,
        params2: bytes,
        intermediate_token: str
    ) -> bytes:
        """
        编码跨协议套利数据
        """
        inner_data = encode(
            ['uint8', 'bytes', 'uint8', 'bytes', 'address'],
            [
                swap_type1.value,
                params1,
                swap_type2.value,
                params2,
                self.w3.to_checksum_address(intermediate_token)
            ]
        )
        return self._encode_user_data(SwapType.CROSS_PROTOCOL, inner_data)
    
    def execute_v3_arbitrage(
        self,
        pool_address: str,
        token_borrow: str,
        amount_borrow: int,
        swap_type: SwapType,
        swap_params: bytes,
        expected_profit: int = 0,
        dry_run: bool = False
    ) -> V3ExecutionResult:
        """
        执行 V3 闪电贷套利
        
        参数：
            pool_address: V3 池地址（闪电贷源）
            token_borrow: 借入的代币
            amount_borrow: 借入数量
            swap_type: 交换类型
            swap_params: 交换参数（已编码）
            expected_profit: 预期利润
            dry_run: 是否只模拟
        
        返回：
            V3ExecutionResult
        """
        start_time = time.time()
        
        try:
            # 1. 检查并格式化地址
            pool_address = self.w3.to_checksum_address(pool_address)
            token_borrow = self.w3.to_checksum_address(token_borrow)
            
            # 2. 编码 userData
            user_data = self._encode_user_data(swap_type, swap_params)
            
            # 3. 获取 Gas 参数
            gas_params = self._get_gas_params(sniper_mode=True)
            
            # 4. 获取 nonce
            nonce = self._get_nonce()
            
            # 5. 构建交易
            tx = self.contract.functions.startArbitrage(
                pool_address,
                token_borrow,
                amount_borrow,
                user_data
            ).build_transaction({
                "from": self.address,
                "nonce": nonce,
                "gas": self.gas_limit,
                **gas_params
            })
            
            if dry_run:
                return V3ExecutionResult(
                    success=True,
                    tx_hash=None,
                    gas_price=gas_params.get("gasPrice", gas_params.get("maxFeePerGas", 0)),
                    error="Dry run - 未实际发送",
                    time_total_ms=(time.time() - start_time) * 1000
                )
            
            # 6. 预执行模拟
            t_sim_start = time.time()
            try:
                call_params = {
                    "from": self.address,
                    "gas": self.gas_limit,
                    **gas_params
                }
                
                self.contract.functions.startArbitrage(
                    pool_address,
                    token_borrow,
                    amount_borrow,
                    user_data
                ).call(call_params)
                
            except Exception as sim_error:
                self._reset_nonce()
                t_sim_end = time.time()
                return V3ExecutionResult(
                    success=False,
                    error=f"Simulation failed: {str(sim_error)}",
                    time_simulation_ms=(t_sim_end - t_sim_start) * 1000,
                    time_total_ms=(time.time() - start_time) * 1000
                )
            t_sim_end = time.time()
            time_sim_ms = (t_sim_end - t_sim_start) * 1000
            
            # 7. 签名交易
            t_sign_start = time.time()
            signed_tx = self.account.sign_transaction(tx)
            t_sign_end = time.time()
            time_sign_ms = (t_sign_end - t_sign_start) * 1000
            
            # 8. 提取原始交易字节（兼容不同 web3 版本）
            raw_tx_bytes = None
            if hasattr(signed_tx, 'rawTransaction'):
                raw_tx_bytes = signed_tx.rawTransaction
            elif hasattr(signed_tx, 'raw_transaction'):
                raw_tx_bytes = signed_tx.raw_transaction
            elif isinstance(signed_tx, dict) and 'rawTransaction' in signed_tx:
                raw_tx_bytes = signed_tx['rawTransaction']
            elif isinstance(signed_tx, dict) and 'raw_transaction' in signed_tx:
                raw_tx_bytes = signed_tx['raw_transaction']
            
            if raw_tx_bytes is None:
                for attr in dir(signed_tx):
                    if 'raw' in attr.lower() and 'transaction' in attr.lower():
                        raw_tx_bytes = getattr(signed_tx, attr, None)
                        if raw_tx_bytes:
                            break
            
            if raw_tx_bytes is None:
                self._reset_nonce()
                return V3ExecutionResult(
                    success=False,
                    error="Could not extract raw transaction bytes",
                    time_simulation_ms=time_sim_ms,
                    time_signing_ms=time_sign_ms,
                    time_total_ms=(time.time() - start_time) * 1000
                )
            
            # 9. 发送交易
            t_broadcast_start = time.time()
            tx_hash = self.w3.eth.send_raw_transaction(raw_tx_bytes)
            tx_hash_hex = tx_hash.hex()
            t_broadcast_end = time.time()
            time_broadcast_ms = (t_broadcast_end - t_broadcast_start) * 1000
            
            # 10. 等待确认
            t_confirm_start = time.time()
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=TX_TIMEOUT)
            t_confirm_end = time.time()
            time_confirm_ms = (t_confirm_end - t_confirm_start) * 1000
            
            # 11. 检查结果
            tx_status = receipt["status"] == 1
            gas_used = receipt["gasUsed"]
            
            self.tx_count += 1
            if tx_status:
                self.success_count += 1
                self.total_profit += expected_profit
            else:
                self.failed_count += 1
            
            return V3ExecutionResult(
                success=tx_status,
                tx_hash=tx_hash_hex,
                gas_used=gas_used,
                gas_price=gas_params.get("gasPrice", gas_params.get("maxFeePerGas", 0)),
                profit_realized=expected_profit if tx_status else 0,
                error=None if tx_status else "Transaction reverted",
                time_simulation_ms=time_sim_ms,
                time_signing_ms=time_sign_ms,
                time_broadcast_ms=time_broadcast_ms,
                time_confirmation_ms=time_confirm_ms,
                time_total_ms=(time.time() - start_time) * 1000
            )
            
        except Exception as e:
            self._reset_nonce()
            self.failed_count += 1
            self.tx_count += 1
            
            return V3ExecutionResult(
                success=False,
                error=str(e),
                time_total_ms=(time.time() - start_time) * 1000
            )
    
    def execute_v3_to_v3_arbitrage(
        self,
        flash_pool: str,
        token_borrow: str,
        amount_borrow: int,
        trade_token_in: str,
        trade_token_out: str,
        trade_fee: int,
        expected_profit: int = 0,
        dry_run: bool = False
    ) -> V3ExecutionResult:
        """
        执行 V3 -> V3 套利
        
        从 flash_pool 借款，在另一个 V3 池交易
        """
        swap_params = self._encode_v3_swap_data(trade_token_in, trade_token_out, trade_fee)
        
        return self.execute_v3_arbitrage(
            pool_address=flash_pool,
            token_borrow=token_borrow,
            amount_borrow=amount_borrow,
            swap_type=SwapType.V3,
            swap_params=swap_params,
            expected_profit=expected_profit,
            dry_run=dry_run
        )
    
    def get_balance(self) -> int:
        """获取账户 ETH 余额"""
        return self.w3.eth.get_balance(self.address)
    
    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
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

def create_v3_executor_from_env(
    w3: Web3,
    contract: Contract
) -> V3ArbitrageExecutor:
    """从环境变量创建 V3 执行器"""
    private_key = os.getenv("PRIVATE_KEY")
    if not private_key:
        raise ValueError("未设置 PRIVATE_KEY 环境变量")
    
    return V3ArbitrageExecutor(w3, contract, private_key)


# ============================================
# 测试代码
# ============================================

if __name__ == "__main__":
    import json
    from pathlib import Path
    from dotenv import load_dotenv
    
    PROJECT_ROOT = Path(__file__).parent.parent
    load_dotenv(PROJECT_ROOT / ".env")
    
    rpc_url = os.getenv("RPC_URL", "http://127.0.0.1:8545")
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    
    if not w3.is_connected():
        print("无法连接到网络")
        exit(1)
    
    print(f"已连接，链 ID: {w3.eth.chain_id}")
    print("\nV3 执行器模块测试完成!")

