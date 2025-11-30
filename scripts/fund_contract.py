#!/usr/bin/env python3
"""
FlashBot 合约注资脚本

用于向机器人合约注入 WETH 以支付闪电贷手续费：
1. 将 ETH 包装成 WETH
2. 将 WETH 转移到机器人合约

⚠️ 此脚本会消耗真实 ETH，请谨慎使用
"""

import os
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account

# 加载环境变量
load_dotenv()


# ============================================================
# 常量配置
# ============================================================

# 机器人合约地址 (已部署)
BOT_CONTRACT_ADDRESS = "0xA4099ADD722ca77c958220171FAa6C9C07674596"

# WETH 合约地址 (Base Mainnet)
WETH_ADDRESS = "0x4200000000000000000000000000000000000006"

# 注资金额 (ETH)
FUND_AMOUNT_ETH = 0.002

# WETH ABI (仅需要 deposit, transfer, balanceOf)
WETH_ABI = [
    {
        "constant": False,
        "inputs": [],
        "name": "deposit",
        "outputs": [],
        "payable": True,
        "stateMutability": "payable",
        "type": "function"
    },
    {
        "constant": False,
        "inputs": [
            {"name": "dst", "type": "address"},
            {"name": "wad", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "payable": False,
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [{"name": "", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "payable": False,
        "stateMutability": "view",
        "type": "function"
    }
]


# ============================================================
# 辅助函数
# ============================================================

def connect_web3() -> tuple[Web3, Account]:
    """
    连接到 Web3 网络
    
    返回:
        (Web3 实例, Account 实例)
    """
    rpc_url = os.getenv("RPC_URL")
    private_key = os.getenv("PRIVATE_KEY")
    
    if not rpc_url:
        raise ValueError("请在 .env 中设置 RPC_URL")
    if not private_key:
        raise ValueError("请在 .env 中设置 PRIVATE_KEY")
    
    print("🌐 连接网络...")
    
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    
    if not w3.is_connected():
        raise ConnectionError("无法连接到 RPC 节点")
    
    chain_id = w3.eth.chain_id
    
    # 显示 RPC URL (隐藏敏感部分)
    display_url = rpc_url[:40] + "..." if len(rpc_url) > 40 else rpc_url
    print(f"   ✅ 已连接")
    print(f"   链 ID: {chain_id}")
    print(f"   RPC: {display_url}")
    
    # 加载账户
    account = Account.from_key(private_key)
    
    return w3, account


def get_balances(w3: Web3, weth_contract, user_address: str, bot_address: str) -> dict:
    """
    获取用户和机器人的余额
    
    参数:
        w3: Web3 实例
        weth_contract: WETH 合约实例
        user_address: 用户地址
        bot_address: 机器人地址
    
    返回:
        余额字典
    """
    user_eth = w3.eth.get_balance(user_address)
    user_weth = weth_contract.functions.balanceOf(user_address).call()
    bot_weth = weth_contract.functions.balanceOf(bot_address).call()
    
    return {
        "user_eth": user_eth,
        "user_weth": user_weth,
        "bot_weth": bot_weth
    }


def print_balances(w3: Web3, balances: dict, label: str):
    """
    打印余额信息
    
    参数:
        w3: Web3 实例
        balances: 余额字典
        label: 标签 (如 "操作前" / "操作后")
    """
    print(f"\n📊 余额 ({label}):")
    print(f"   👤 用户 ETH:  {w3.from_wei(balances['user_eth'], 'ether'):.6f} ETH")
    print(f"   👤 用户 WETH: {w3.from_wei(balances['user_weth'], 'ether'):.6f} WETH")
    print(f"   🤖 机器人 WETH: {w3.from_wei(balances['bot_weth'], 'ether'):.6f} WETH")


def wait_for_weth_balance(
    w3: Web3,
    weth_contract,
    user_address: str,
    required_amount: int,
    timeout: int = 30,
    check_interval: int = 2
) -> bool:
    """
    等待用户的 WETH 余额达到要求的金额
    
    用于解决 RPC 延迟问题：deposit 交易确认后，节点可能还未更新余额
    
    参数:
        w3: Web3 实例
        weth_contract: WETH 合约实例
        user_address: 用户地址
        required_amount: 需要的最小余额 (wei)
        timeout: 超时时间 (秒)
        check_interval: 检查间隔 (秒)
    
    返回:
        是否在超时前达到要求的余额
    """
    print(f"\n⏳ 等待 WETH 余额更新...")
    print(f"   需要: {w3.from_wei(required_amount, 'ether'):.6f} WETH")
    print(f"   超时: {timeout} 秒")
    
    start_time = time.time()
    check_count = 0
    
    while True:
        check_count += 1
        current_balance = weth_contract.functions.balanceOf(user_address).call()
        elapsed = time.time() - start_time
        
        print(f"   [{check_count}] 当前余额: {w3.from_wei(current_balance, 'ether'):.6f} WETH (已等待 {elapsed:.1f}s)")
        
        if current_balance >= required_amount:
            print(f"   ✅ 余额已确认!")
            return True
        
        if elapsed >= timeout:
            print(f"   ❌ 超时! 余额未更新")
            return False
        
        time.sleep(check_interval)
    
    return False


def wrap_eth(w3: Web3, account: Account, weth_contract, amount_wei: int) -> bool:
    """
    将 ETH 包装成 WETH
    
    参数:
        w3: Web3 实例
        account: 账户
        weth_contract: WETH 合约实例
        amount_wei: 金额 (wei)
    
    返回:
        是否成功
    """
    print(f"\n💱 包装 ETH -> WETH...")
    print(f"   金额: {w3.from_wei(amount_wei, 'ether')} ETH")
    
    try:
        # 获取 nonce
        nonce = w3.eth.get_transaction_count(account.address, 'pending')
        print(f"   Nonce: {nonce}")
        
        # 估算 gas
        gas_estimate = weth_contract.functions.deposit().estimate_gas({
            "from": account.address,
            "value": amount_wei
        })
        print(f"   预估 Gas: {gas_estimate:,}")
        
        # 获取 gas 价格
        gas_price = w3.eth.gas_price
        print(f"   Gas 价格: {w3.from_wei(gas_price, 'gwei'):.4f} Gwei")
        
        # 构建交易
        tx = weth_contract.functions.deposit().build_transaction({
            "from": account.address,
            "value": amount_wei,
            "nonce": nonce,
            "gas": int(gas_estimate * 1.2),
            "gasPrice": gas_price,
        })
        
        # 签名并发送
        signed_tx = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        
        print(f"   交易哈希: {tx_hash.hex()}")
        print(f"   等待确认...")
        
        # 等待确认
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt["status"] == 1:
            print(f"   ✅ 包装成功!")
            print(f"   使用 Gas: {receipt['gasUsed']:,}")
            return True
        else:
            print(f"   ❌ 包装失败 (交易回滚)")
            return False
            
    except Exception as e:
        print(f"   ❌ 错误: {e}")
        return False


def transfer_weth(
    w3: Web3, 
    account: Account, 
    weth_contract, 
    to_address: str, 
    amount_wei: int
) -> bool:
    """
    转移 WETH 到目标地址
    
    参数:
        w3: Web3 实例
        account: 账户
        weth_contract: WETH 合约实例
        to_address: 目标地址
        amount_wei: 金额 (wei)
    
    返回:
        是否成功
    """
    to_address = w3.to_checksum_address(to_address)
    
    print(f"\n📤 转移 WETH 到机器人...")
    print(f"   目标: {to_address}")
    print(f"   金额: {w3.from_wei(amount_wei, 'ether')} WETH")
    
    try:
        # 获取 nonce
        nonce = w3.eth.get_transaction_count(account.address, 'pending')
        print(f"   Nonce: {nonce}")
        
        # 估算 gas
        gas_estimate = weth_contract.functions.transfer(
            to_address,
            amount_wei
        ).estimate_gas({"from": account.address})
        print(f"   预估 Gas: {gas_estimate:,}")
        
        # 获取 gas 价格
        gas_price = w3.eth.gas_price
        print(f"   Gas 价格: {w3.from_wei(gas_price, 'gwei'):.4f} Gwei")
        
        # 构建交易
        tx = weth_contract.functions.transfer(
            to_address,
            amount_wei
        ).build_transaction({
            "from": account.address,
            "nonce": nonce,
            "gas": int(gas_estimate * 1.2),
            "gasPrice": gas_price,
        })
        
        # 签名并发送
        signed_tx = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
        
        print(f"   交易哈希: {tx_hash.hex()}")
        print(f"   等待确认...")
        
        # 等待确认
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        
        if receipt["status"] == 1:
            print(f"   ✅ 转移成功!")
            print(f"   使用 Gas: {receipt['gasUsed']:,}")
            return True
        else:
            print(f"   ❌ 转移失败 (交易回滚)")
            return False
            
    except Exception as e:
        print(f"   ❌ 错误: {e}")
        return False


# ============================================================
# 主函数
# ============================================================

def main():
    """主函数"""
    print("=" * 60)
    print("💰 FlashBot 合约注资脚本")
    print("=" * 60)
    
    amount_wei = Web3.to_wei(FUND_AMOUNT_ETH, 'ether')
    
    print(f"\n📋 配置:")
    print(f"   机器人地址: {BOT_CONTRACT_ADDRESS}")
    print(f"   WETH 合约: {WETH_ADDRESS}")
    print(f"   注资金额: {FUND_AMOUNT_ETH} ETH")
    
    # ===== 1. 连接网络 =====
    print()
    w3, account = connect_web3()
    
    print(f"\n👛 账户:")
    print(f"   地址: {account.address}")
    
    # ===== 2. 初始化合约 =====
    weth_address = w3.to_checksum_address(WETH_ADDRESS)
    bot_address = w3.to_checksum_address(BOT_CONTRACT_ADDRESS)
    weth_contract = w3.eth.contract(address=weth_address, abi=WETH_ABI)
    
    # ===== 3. 显示操作前余额 =====
    balances_before = get_balances(w3, weth_contract, account.address, bot_address)
    print_balances(w3, balances_before, "操作前")
    
    # 检查余额是否足够
    if balances_before["user_eth"] < amount_wei:
        print(f"\n❌ ETH 余额不足!")
        print(f"   需要: {w3.from_wei(amount_wei, 'ether')} ETH")
        print(f"   当前: {w3.from_wei(balances_before['user_eth'], 'ether')} ETH")
        sys.exit(1)
    
    # ===== 4. 包装 ETH -> WETH =====
    if not wrap_eth(w3, account, weth_contract, amount_wei):
        print("\n❌ 包装失败，操作中止")
        sys.exit(1)
    
    # ===== 4.5 等待余额更新 (解决 RPC 延迟问题) =====
    if not wait_for_weth_balance(w3, weth_contract, account.address, amount_wei):
        print("\n❌ WETH 余额未更新，操作中止")
        print("⚠️ 注意: WETH 可能已在你的钱包中，请稍后手动检查并转移")
        sys.exit(1)
    
    # ===== 5. 转移 WETH 到机器人 =====
    if not transfer_weth(w3, account, weth_contract, bot_address, amount_wei):
        print("\n❌ 转移失败，操作中止")
        print("⚠️ 注意: WETH 仍在你的钱包中，可以稍后手动转移")
        sys.exit(1)
    
    # ===== 6. 显示操作后余额 =====
    balances_after = get_balances(w3, weth_contract, account.address, bot_address)
    print_balances(w3, balances_after, "操作后")
    
    # ===== 7. 显示变化 =====
    print(f"\n📈 余额变化:")
    eth_change = balances_after["user_eth"] - balances_before["user_eth"]
    weth_change = balances_after["bot_weth"] - balances_before["bot_weth"]
    print(f"   用户 ETH: {w3.from_wei(eth_change, 'ether'):+.6f} ETH (包含 gas 费)")
    print(f"   机器人 WETH: {w3.from_wei(weth_change, 'ether'):+.6f} WETH")
    
    # ===== 完成 =====
    print("\n" + "=" * 60)
    print("🎉 注资完成!")
    print("=" * 60)
    print(f"\n📋 摘要:")
    print(f"   机器人现有 WETH: {w3.from_wei(balances_after['bot_weth'], 'ether'):.6f} WETH")
    print(f"\n📝 下一步:")
    print(f"   运行主程序: python main.py")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n⚠️ 用户取消操作")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

