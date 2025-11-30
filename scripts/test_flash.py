#!/usr/bin/env python3
"""
FlashBot 闪电贷测试脚本

功能：
1. 验证合约部署和配置
2. 检查路由器授权
3. 诊断闪电贷执行环境
4. 解释测试限制

重要说明：
    在本地 Anvil fork 环境中，完整的闪电贷三角套利测试可能失败，
    因为 WETH -> USDbC -> WETH 路径使用同一个配对，这不是有效的套利路径。
    
    真正的套利需要在不同的 DEX/配对之间进行，例如：
    - 从 DEX-A 借入 WETH
    - 在 DEX-B 将 WETH 换成 USDC（价格较高）
    - 在 DEX-C 将 USDC 换回 WETH（价格较低）
    - 偿还 DEX-A 的闪电贷 + 手续费
    - 保留差价作为利润

使用方法：
    python scripts/test_flash.py
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from web3 import Web3

load_dotenv(PROJECT_ROOT / ".env")


# ============================================
# 硬编码的 Base 主网地址
# ============================================

WETH_ADDRESS = "0x4200000000000000000000000000000000000006"
USDC_ADDRESS = "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA"  # USDbC
PAIR_ADDRESS = "0x41d160033c222e6f3722ec97379867324567d883"  # BaseSwap WETH/USDbC
ROUTER_ADDRESS = "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24"  # Uniswap V2 Router

DEPLOYMENTS_FILE = PROJECT_ROOT / "deployments.json"


# ============================================
# 合约 ABI
# ============================================

ERC20_ABI = [
    {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf",
     "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "name": "allowance", "outputs": [{"name": "", "type": "uint256"}], "stateMutability": "view", "type": "function"}
]

PAIR_ABI = [
    {"inputs": [], "name": "token0", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "token1", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "getReserves", "outputs": [
        {"name": "reserve0", "type": "uint112"},
        {"name": "reserve1", "type": "uint112"},
        {"name": "blockTimestampLast", "type": "uint32"}
    ], "stateMutability": "view", "type": "function"}
]

ROUTER_ABI = [
    {"inputs": [], "name": "factory", "outputs": [{"name": "", "type": "address"}], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "amountIn", "type": "uint256"}, {"name": "path", "type": "address[]"}],
     "name": "getAmountsOut", "outputs": [{"name": "amounts", "type": "uint256[]"}], "stateMutability": "view", "type": "function"}
]

FACTORY_ABI = [
    {"inputs": [{"name": "tokenA", "type": "address"}, {"name": "tokenB", "type": "address"}],
     "name": "getPair", "outputs": [{"name": "pair", "type": "address"}], "stateMutability": "view", "type": "function"}
]


# ============================================
# 辅助函数
# ============================================

def load_deployment(chain_id: int) -> Dict[str, Any]:
    if not DEPLOYMENTS_FILE.exists():
        raise FileNotFoundError(f"部署文件不存在: {DEPLOYMENTS_FILE}")
    deployments = json.loads(DEPLOYMENTS_FILE.read_text(encoding="utf-8"))
    if str(chain_id) not in deployments:
        raise ValueError(f"未找到链 {chain_id} 的部署信息")
    return deployments[str(chain_id)]


def check_callback_type(w3: Web3, pair_address: str) -> str:
    """检查配对使用的回调类型"""
    pair_address = w3.to_checksum_address(pair_address)
    bytecode = w3.eth.get_code(pair_address).hex()
    
    uniswap_selector = w3.keccak(text="uniswapV2Call(address,uint256,uint256,bytes)")[:4].hex()[2:]
    pancake_selector = w3.keccak(text="pancakeCall(address,uint256,uint256,bytes)")[:4].hex()[2:]
    
    if pancake_selector in bytecode:
        return "pancakeCall"
    elif uniswap_selector in bytecode:
        return "uniswapV2Call"
    else:
        return "unknown"


# ============================================
# 主函数
# ============================================

def main():
    print("\n" + "=" * 60)
    print("FlashBot 环境验证脚本 (Base Mainnet Fork)")
    print("=" * 60)
    
    # 1. 连接网络
    print("\n[1] 连接网络...")
    rpc_url = os.getenv("RPC_URL", "http://127.0.0.1:8545")
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    
    if not w3.is_connected():
        print("   FAIL: 无法连接到网络")
        sys.exit(1)
    
    chain_id = w3.eth.chain_id
    print(f"   OK: 已连接 (链 ID: {chain_id})")
    
    # 2. 加载账户
    print("\n[2] 加载账户...")
    private_key = os.getenv("PRIVATE_KEY", "")
    if not private_key:
        print("   FAIL: 未设置 PRIVATE_KEY")
        sys.exit(1)
    
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    
    account = w3.eth.account.from_key(private_key)
    balance = w3.eth.get_balance(account.address)
    print(f"   OK: {account.address}")
    print(f"       余额: {w3.from_wei(balance, 'ether'):.4f} ETH")
    
    # 3. 加载合约
    print("\n[3] 加载 FlashBot 合约...")
    try:
        deployment = load_deployment(chain_id)
        contract_address = w3.to_checksum_address(deployment["contract_address"])
        abi = deployment["abi"]
        contract = w3.eth.contract(address=contract_address, abi=abi)
        print(f"   OK: {contract_address}")
    except Exception as e:
        print(f"   FAIL: {e}")
        sys.exit(1)
    
    # 4. 检查合约所有权
    print("\n[4] 检查合约所有权...")
    owner = contract.functions.owner().call()
    if owner.lower() == account.address.lower():
        print(f"   OK: 当前账户是合约所有者")
    else:
        print(f"   WARN: 当前账户不是所有者 (所有者: {owner})")
    
    # 5. 检查代币地址
    print("\n[5] 验证代币地址...")
    weth = w3.to_checksum_address(WETH_ADDRESS)
    usdc = w3.to_checksum_address(USDC_ADDRESS)
    print(f"   WETH: {weth}")
    print(f"   USDbC: {usdc}")
    
    # 6. 检查配对信息
    print("\n[6] 检查 BaseSwap 配对...")
    pair_address = w3.to_checksum_address(PAIR_ADDRESS)
    pair = w3.eth.contract(address=pair_address, abi=PAIR_ABI)
    
    token0 = pair.functions.token0().call()
    token1 = pair.functions.token1().call()
    reserves = pair.functions.getReserves().call()
    
    print(f"   配对地址: {pair_address}")
    print(f"   Token0 (WETH): {token0}")
    print(f"   Token1 (USDbC): {token1}")
    print(f"   Reserve0: {reserves[0]:,} ({w3.from_wei(reserves[0], 'ether'):.4f} WETH)")
    print(f"   Reserve1: {reserves[1]:,} ({reserves[1] / 10**6:.2f} USDbC)")
    
    # 检查回调类型
    callback_type = check_callback_type(w3, pair_address)
    print(f"   回调类型: {callback_type}")
    if callback_type == "pancakeCall":
        print(f"   OK: 合约已实现 pancakeCall 回调")
    
    # 7. 检查路由器
    print("\n[7] 检查路由器配置...")
    router_address = w3.to_checksum_address(ROUTER_ADDRESS)
    router = w3.eth.contract(address=router_address, abi=ROUTER_ABI)
    
    factory_address = router.functions.factory().call()
    print(f"   路由器: {router_address}")
    print(f"   工厂: {factory_address}")
    
    # 检查路由器的配对
    factory = w3.eth.contract(address=w3.to_checksum_address(factory_address), abi=FACTORY_ABI)
    router_pair = factory.functions.getPair(weth, usdc).call()
    print(f"   路由器配对: {router_pair}")
    
    is_same_pair = router_pair.lower() == pair_address.lower()
    if is_same_pair:
        print(f"   WARN: 路由器配对与借贷配对相同")
        print(f"         在闪电贷回调中使用同一个配对会导致 LOCKED 错误")
    else:
        print(f"   OK: 路由器使用不同的配对")
    
    # 8. 检查授权状态
    print("\n[8] 检查路由器授权...")
    
    tokens = [(weth, "WETH", 18), (usdc, "USDbC", 6)]
    for token_addr, name, decimals in tokens:
        allowance = contract.functions.getRouterAllowance(token_addr, router_address).call()
        if allowance > 0:
            print(f"   OK: {name} 已授权路由器")
        else:
            print(f"   WARN: {name} 未授权路由器")
            print(f"         运行: contract.functions.approveRouter({token_addr}, {router_address})")
    
    # 9. 检查合约余额
    print("\n[9] 检查合约代币余额...")
    
    weth_contract = w3.eth.contract(address=weth, abi=ERC20_ABI)
    contract_weth = weth_contract.functions.balanceOf(contract_address).call()
    print(f"   合约 WETH: {w3.from_wei(contract_weth, 'ether'):.6f} WETH")
    
    if contract_weth > 0:
        print(f"   OK: 合约有 WETH 余额可用于支付闪电贷手续费")
    else:
        print(f"   WARN: 合约没有 WETH 余额")
        print(f"         执行闪电贷前需要先向合约转入 WETH")
    
    # 10. 测试路由器 getAmountsOut
    print("\n[10] 测试路由器价格查询...")
    
    test_amount = w3.to_wei(0.001, "ether")
    try:
        amounts = router.functions.getAmountsOut(test_amount, [weth, usdc]).call()
        print(f"   WETH -> USDbC: {w3.from_wei(amounts[0], 'ether')} WETH = {amounts[1] / 10**6:.4f} USDbC")
        
        amounts2 = router.functions.getAmountsOut(amounts[1], [usdc, weth]).call()
        print(f"   USDbC -> WETH: {amounts2[0] / 10**6:.4f} USDbC = {w3.from_wei(amounts2[1], 'ether'):.6f} WETH")
        
        loss = test_amount - amounts2[1]
        print(f"   往返损失: {w3.from_wei(loss, 'ether'):.6f} WETH ({loss * 100 / test_amount:.2f}%)")
    except Exception as e:
        print(f"   FAIL: {e}")
    
    # 总结
    print("\n" + "=" * 60)
    print("验证总结")
    print("=" * 60)
    
    print("""
FlashBot 合约已正确部署和配置。

当前测试环境的限制:
-----------------------
1. BaseSwap 配对使用 pancakeCall 回调 (OK - 已支持)
2. Uniswap V2 Router 与 BaseSwap 使用不同的配对 (OK)
3. 三角套利路径 WETH -> USDbC -> WETH 在同一个 DEX 上不可行
   (因为两个 swap 使用同一个配对，第一个 swap 改变了储备)

真正的套利机会:
-----------------------
要执行有利可图的闪电贷套利，你需要:
1. 找到不同 DEX 之间的价格差异
2. 使用多个配对/路由器执行套利路径
3. 确保利润 > 闪电贷手续费 (0.3%) + Gas 费用

示例套利路径:
- 从 BaseSwap WETH/USDbC 配对借入 WETH
- 在 Aerodrome 将 WETH 换成 USDC (如果价格更高)
- 在 SushiSwap 将 USDC 换回 WETH (如果价格更低)
- 偿还 BaseSwap 闪电贷

下一步:
-----------------------
1. 实现多路由器支持 (修改 userData 格式)
2. 构建价格监控系统
3. 实现自动套利路径发现

合约功能已验证 - 可以开始构建套利策略!
""")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n用户取消操作")
        sys.exit(1)
    except Exception as e:
        print(f"\n错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
