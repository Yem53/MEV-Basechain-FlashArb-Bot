#!/usr/bin/env python3
"""
Multicall 批量调用辅助模块

功能：
- 使用 Multicall3 合约在一次 RPC 请求中批量调用多个合约函数
- 大幅减少 RPC 调用次数，提高扫描性能
- 支持批量获取多个配对的储备数据

Multicall3 地址（Base Mainnet）：0xcA11bde05977b3631167028862bE2a173976CA11

使用示例：
    multicall = Multicall(w3)
    results = multicall.aggregate([
        (pair1_address, get_reserves_data),
        (pair2_address, get_reserves_data),
    ])
"""

from typing import Any, List, Tuple, Optional
from web3 import Web3
from eth_abi import encode, decode


# Multicall3 合约地址（所有 EVM 链通用）
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"

# Multicall3 ABI（只需要 aggregate3 函数）
MULTICALL3_ABI = [
    {
        "inputs": [
            {
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "allowFailure", "type": "bool"},
                    {"name": "callData", "type": "bytes"}
                ],
                "name": "calls",
                "type": "tuple[]"
            }
        ],
        "name": "aggregate3",
        "outputs": [
            {
                "components": [
                    {"name": "success", "type": "bool"},
                    {"name": "returnData", "type": "bytes"}
                ],
                "name": "returnData",
                "type": "tuple[]"
            }
        ],
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "inputs": [
            {
                "components": [
                    {"name": "target", "type": "address"},
                    {"name": "callData", "type": "bytes"}
                ],
                "name": "calls",
                "type": "tuple[]"
            }
        ],
        "name": "aggregate",
        "outputs": [
            {"name": "blockNumber", "type": "uint256"},
            {"name": "returnData", "type": "bytes[]"}
        ],
        "stateMutability": "payable",
        "type": "function"
    }
]

# getReserves() 函数选择器
GET_RESERVES_SELECTOR = "0x0902f1ac"


class Multicall:
    """
    Multicall 批量调用辅助类
    
    使用 Multicall3 合约在一次 RPC 请求中执行多个 view/pure 函数调用，
    大幅减少网络延迟和 RPC 调用次数。
    """
    
    def __init__(self, w3: Web3, address: str = MULTICALL3_ADDRESS):
        """
        初始化 Multicall 实例
        
        参数：
            w3: Web3 实例
            address: Multicall3 合约地址（默认为标准地址）
        """
        self.w3 = w3
        self.address = w3.to_checksum_address(address)
        self.contract = w3.eth.contract(
            address=self.address,
            abi=MULTICALL3_ABI
        )
    
    def aggregate(
        self,
        calls: List[Tuple[str, bytes]],
        allow_failure: bool = True
    ) -> List[Tuple[bool, bytes]]:
        """
        批量执行多个合约调用
        
        参数：
            calls: 调用列表，每个元素为 (目标合约地址, 调用数据)
            allow_failure: 是否允许单个调用失败
            
        返回：
            结果列表，每个元素为 (是否成功, 返回数据)
        """
        # 构建 aggregate3 调用参数
        formatted_calls = [
            (
                self.w3.to_checksum_address(target),
                allow_failure,
                call_data if isinstance(call_data, bytes) else bytes.fromhex(call_data.replace("0x", ""))
            )
            for target, call_data in calls
        ]
        
        # 执行批量调用
        try:
            results = self.contract.functions.aggregate3(formatted_calls).call()
            return [(r[0], r[1]) for r in results]
        except Exception as e:
            # 如果 aggregate3 失败，尝试使用简单的 aggregate
            return self._aggregate_fallback(calls)
    
    def _aggregate_fallback(
        self,
        calls: List[Tuple[str, bytes]]
    ) -> List[Tuple[bool, bytes]]:
        """
        使用简单的 aggregate 函数作为后备方案
        
        参数：
            calls: 调用列表
            
        返回：
            结果列表
        """
        formatted_calls = [
            (
                self.w3.to_checksum_address(target),
                call_data if isinstance(call_data, bytes) else bytes.fromhex(call_data.replace("0x", ""))
            )
            for target, call_data in calls
        ]
        
        try:
            _, return_data = self.contract.functions.aggregate(formatted_calls).call()
            return [(True, data) for data in return_data]
        except Exception:
            # 如果全部失败，返回空结果
            return [(False, b"") for _ in calls]
    
    def get_reserves_batch(
        self,
        pair_addresses: List[str]
    ) -> List[Optional[Tuple[int, int, int]]]:
        """
        批量获取多个配对的储备数据
        
        参数：
            pair_addresses: 配对合约地址列表
            
        返回：
            储备数据列表，每个元素为 (reserve0, reserve1, timestamp) 或 None（如果失败）
        """
        # 构建 getReserves 调用
        calls = [
            (addr, GET_RESERVES_SELECTOR)
            for addr in pair_addresses
        ]
        
        # 执行批量调用
        results = self.aggregate(calls)
        
        # 解码结果
        reserves_list = []
        for success, return_data in results:
            if success and len(return_data) >= 96:  # 3 * 32 bytes
                try:
                    # 解码 (uint112 reserve0, uint112 reserve1, uint32 blockTimestampLast)
                    decoded = decode(
                        ["uint112", "uint112", "uint32"],
                        return_data
                    )
                    reserves_list.append((decoded[0], decoded[1], decoded[2]))
                except Exception:
                    reserves_list.append(None)
            else:
                reserves_list.append(None)
        
        return reserves_list
    
    def get_token_balances_batch(
        self,
        token_address: str,
        account_addresses: List[str]
    ) -> List[Optional[int]]:
        """
        批量获取多个地址的代币余额
        
        参数：
            token_address: 代币合约地址
            account_addresses: 账户地址列表
            
        返回：
            余额列表，每个元素为余额或 None（如果失败）
        """
        # balanceOf(address) 选择器
        balance_of_selector = "0x70a08231"
        
        # 构建调用
        calls = []
        for account in account_addresses:
            # 编码参数
            encoded_account = encode(["address"], [self.w3.to_checksum_address(account)])
            call_data = bytes.fromhex(balance_of_selector[2:]) + encoded_account
            calls.append((token_address, call_data))
        
        # 执行批量调用
        results = self.aggregate(calls)
        
        # 解码结果
        balances = []
        for success, return_data in results:
            if success and len(return_data) >= 32:
                try:
                    decoded = decode(["uint256"], return_data)
                    balances.append(decoded[0])
                except Exception:
                    balances.append(None)
            else:
                balances.append(None)
        
        return balances


def create_get_reserves_call(pair_address: str) -> Tuple[str, bytes]:
    """
    创建 getReserves 调用数据
    
    参数：
        pair_address: 配对合约地址
        
    返回：
        (目标地址, 调用数据) 元组
    """
    return (pair_address, bytes.fromhex(GET_RESERVES_SELECTOR[2:]))


def decode_reserves(return_data: bytes) -> Optional[Tuple[int, int, int]]:
    """
    解码 getReserves 返回数据
    
    参数：
        return_data: 原始返回数据
        
    返回：
        (reserve0, reserve1, timestamp) 或 None
    """
    if len(return_data) < 96:
        return None
    
    try:
        decoded = decode(["uint112", "uint112", "uint32"], return_data)
        return (decoded[0], decoded[1], decoded[2])
    except Exception:
        return None


# ============================================
# 测试代码
# ============================================

if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    from pathlib import Path
    
    # 加载环境变量
    load_dotenv(Path(__file__).parent.parent / ".env")
    
    # 连接到本地 fork
    w3 = Web3(Web3.HTTPProvider(os.getenv("RPC_URL", "http://127.0.0.1:8545")))
    
    if not w3.is_connected():
        print("无法连接到网络")
        exit(1)
    
    print(f"已连接到网络，链 ID: {w3.eth.chain_id}")
    
    # 创建 Multicall 实例
    multicall = Multicall(w3)
    
    # 测试配对地址（Base Mainnet）
    test_pairs = [
        "0x41d160033C222E6f3722EC97379867324567d883",  # BaseSwap WETH/USDbC
        "0xe902EF54E437967c8b37D30E80ff887955c90DB6",  # Uniswap V2 WETH/USDbC
    ]
    
    print("\n批量获取配对储备...")
    reserves = multicall.get_reserves_batch(test_pairs)
    
    for i, (pair, reserve) in enumerate(zip(test_pairs, reserves)):
        print(f"\n配对 {i + 1}: {pair}")
        if reserve:
            r0, r1, ts = reserve
            print(f"  Reserve0: {r0:,}")
            print(f"  Reserve1: {r1:,}")
            print(f"  Timestamp: {ts}")
        else:
            print("  获取失败")
    
    print("\n测试完成!")

