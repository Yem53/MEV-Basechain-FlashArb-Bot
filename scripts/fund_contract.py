#!/usr/bin/env python3
"""
FlashBot 合约注资脚本 (Smart Version)

智能注资逻辑：
1. 检查钱包现有的 WETH 余额
2. 如果有足够的 WETH，直接转移到合约（无需包装）
3. 如果 WETH 不足，先包装 ETH 再转移

⚠️ 此脚本会消耗真实资金，请仔细确认地址
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
# 从环境变量加载配置（不再硬编码）
# ============================================================

# WETH 合约地址 (Base Mainnet)
WETH_ADDRESS = os.getenv("WETH", "0x4200000000000000000000000000000000000006")

# 默认注资金额 (ETH) - 可通过环境变量覆盖
DEFAULT_FUND_AMOUNT_ETH = float(os.getenv("FUND_AMOUNT_ETH", "0.002"))

# 最小 WETH 阈值 - 超过此值时询问是否直接转移
MIN_WETH_THRESHOLD_ETH = float(os.getenv("MIN_WETH_THRESHOLD_ETH", "0.002"))


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

def get_raw_transaction(signed_tx):
    """
    兼容 Web3.py 不同版本的 rawTransaction 获取方式
    """
    if hasattr(signed_tx, 'raw_transaction'):
        return signed_tx.raw_transaction
    elif hasattr(signed_tx, 'rawTransaction'):
        return signed_tx.rawTransaction
    else:
        raise AttributeError("无法获取 raw transaction，请检查 Web3.py 版本")


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
    """
    user_eth = w3.eth.get_balance(user_address)
    user_weth = weth_contract.functions.balanceOf(user_address).call()
    bot_weth = weth_contract.functions.balanceOf(bot_address).call()
    bot_eth = w3.eth.get_balance(bot_address)
    
    return {
        "user_eth": user_eth,
        "user_weth": user_weth,
        "bot_weth": bot_weth,
        "bot_eth": bot_eth
    }


def print_balances(w3: Web3, balances: dict, label: str):
    """
    打印余额信息
    """
    print(f"\n📊 余额 ({label}):")
    print(f"   👤 钱包 ETH:    {w3.from_wei(balances['user_eth'], 'ether'):.6f} ETH")
    print(f"   👤 钱包 WETH:   {w3.from_wei(balances['user_weth'], 'ether'):.6f} WETH")
    print(f"   🤖 合约 WETH:   {w3.from_wei(balances['bot_weth'], 'ether'):.6f} WETH")
    print(f"   🤖 合约 ETH:    {w3.from_wei(balances['bot_eth'], 'ether'):.6f} ETH")


def wrap_eth(w3: Web3, account: Account, weth_contract, amount_wei: int) -> bool:
    """
    将 ETH 包装成 WETH
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
        raw_tx = get_raw_transaction(signed_tx)
        tx_hash = w3.eth.send_raw_transaction(raw_tx)
        
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
    """
    to_address = w3.to_checksum_address(to_address)
    
    print(f"\n📤 转移 WETH 到合约...")
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
        raw_tx = get_raw_transaction(signed_tx)
        tx_hash = w3.eth.send_raw_transaction(raw_tx)
        
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


def ask_user_choice(prompt: str, default: str = "n") -> bool:
    """
    询问用户确认
    """
    try:
        response = input(f"{prompt} [{default.upper() if default == 'y' else 'y'}/{default.upper() if default == 'n' else 'n'}]: ").strip().lower()
        if not response:
            response = default
        return response == 'y' or response == 'yes'
    except (EOFError, KeyboardInterrupt):
        return False


def ask_amount(prompt: str, default: float, max_amount: float) -> float:
    """
    询问用户输入金额
    """
    try:
        response = input(f"{prompt} (默认: {default}, 最大: {max_amount:.6f}): ").strip()
        if not response:
            return default
        
        amount = float(response)
        if amount <= 0:
            print("   ⚠️ 金额必须大于 0，使用默认值")
            return default
        if amount > max_amount:
            print(f"   ⚠️ 金额超过最大值，使用 {max_amount:.6f}")
            return max_amount
        return amount
    except (ValueError, EOFError, KeyboardInterrupt):
        return default


# ============================================================
# 主函数
# ============================================================

def main():
    """主函数"""
    print("=" * 60)
    print("💰 FlashBot 合约注资脚本 (Smart Version)")
    print("=" * 60)
    
    # ===== 1. 从 .env 加载配置 =====
    flashbot_address = os.getenv("FLASHBOT_ADDRESS")
    if not flashbot_address:
        print("\n❌ 错误: 请在 .env 中设置 FLASHBOT_ADDRESS")
        print("   这是你的 V3 机器人合约地址")
        sys.exit(1)
    
    print(f"\n📋 配置 (从 .env 加载):")
    print(f"   合约地址 (FLASHBOT_ADDRESS): {flashbot_address}")
    print(f"   WETH 合约: {WETH_ADDRESS}")
    print(f"   默认注资金额: {DEFAULT_FUND_AMOUNT_ETH} ETH")
    print(f"   WETH 阈值: {MIN_WETH_THRESHOLD_ETH} ETH")
    
    # ===== 2. 连接网络 =====
    print()
    w3, account = connect_web3()
    
    # 安全检查 - 显示关键地址
    print("\n" + "=" * 60)
    print("⚠️  安全检查 - 请确认以下地址正确")
    print("=" * 60)
    print(f"   👤 你的钱包:      {account.address}")
    print(f"   🤖 目标合约:      {flashbot_address}")
    print("=" * 60)
    
    # ===== 3. 初始化合约 =====
    weth_address = w3.to_checksum_address(WETH_ADDRESS)
    bot_address = w3.to_checksum_address(flashbot_address)
    weth_contract = w3.eth.contract(address=weth_address, abi=WETH_ABI)
    
    # ===== 4. 显示当前余额 =====
    balances = get_balances(w3, weth_contract, account.address, bot_address)
    print_balances(w3, balances, "当前")
    
    user_weth = balances["user_weth"]
    user_eth = balances["user_eth"]
    user_weth_eth = float(w3.from_wei(user_weth, 'ether'))
    user_eth_eth = float(w3.from_wei(user_eth, 'ether'))
    
    # ===== 5. 智能逻辑：检查钱包 WETH 余额 =====
    threshold_wei = Web3.to_wei(MIN_WETH_THRESHOLD_ETH, 'ether')
    
    if user_weth >= threshold_wei:
        # 钱包有足够的 WETH
        print(f"\n💡 检测到钱包有 {user_weth_eth:.6f} WETH (>= {MIN_WETH_THRESHOLD_ETH} 阈值)")
        print(f"   可以直接转移现有 WETH，无需包装 ETH")
        
        if ask_user_choice("\n是否转移现有 WETH 到合约?", "y"):
            # 询问转移金额
            transfer_amount_eth = ask_amount(
                "   输入转移金额 (ETH)", 
                min(user_weth_eth, DEFAULT_FUND_AMOUNT_ETH),
                user_weth_eth
            )
            transfer_amount_wei = Web3.to_wei(transfer_amount_eth, 'ether')
            
            print(f"\n📝 即将转移 {transfer_amount_eth:.6f} WETH 到 {bot_address[:20]}...")
            
            if ask_user_choice("确认执行?", "y"):
                if transfer_weth(w3, account, weth_contract, bot_address, transfer_amount_wei):
                    # 成功 - 显示结果
                    balances_after = get_balances(w3, weth_contract, account.address, bot_address)
                    print_balances(w3, balances_after, "操作后")
                    
                    weth_change = balances_after["bot_weth"] - balances["bot_weth"]
                    print(f"\n📈 合约 WETH: +{w3.from_wei(weth_change, 'ether'):.6f} WETH")
                    
                    print("\n" + "=" * 60)
                    print("🎉 注资完成!")
                    print("=" * 60)
                    print(f"\n📋 摘要:")
                    print(f"   合约现有 WETH: {w3.from_wei(balances_after['bot_weth'], 'ether'):.6f} WETH")
                    print(f"\n📝 下一步:")
                    print(f"   运行主程序: python main.py")
                else:
                    print("\n❌ 转移失败")
                    sys.exit(1)
            else:
                print("\n⚠️ 用户取消操作")
                sys.exit(0)
        else:
            print("\n⚠️ 用户选择不转移")
            # 询问是否使用包装流程
            if ask_user_choice("是否使用 ETH 包装流程?", "n"):
                _do_wrap_and_transfer(w3, account, weth_contract, bot_address, balances)
            else:
                sys.exit(0)
    
    else:
        # 钱包 WETH 不足，使用包装流程
        print(f"\n💡 钱包 WETH ({user_weth_eth:.6f}) 低于阈值 ({MIN_WETH_THRESHOLD_ETH})")
        print(f"   将使用 ETH 包装流程")
        
        if user_eth < Web3.to_wei(DEFAULT_FUND_AMOUNT_ETH, 'ether'):
            print(f"\n⚠️ ETH 余额也不足 ({user_eth_eth:.6f} < {DEFAULT_FUND_AMOUNT_ETH})")
            
            if user_weth > 0:
                print(f"   但是你有 {user_weth_eth:.6f} WETH，可以转移这些")
                if ask_user_choice("是否转移现有 WETH?", "y"):
                    transfer_amount_wei = user_weth  # 全部转移
                    if transfer_weth(w3, account, weth_contract, bot_address, transfer_amount_wei):
                        balances_after = get_balances(w3, weth_contract, account.address, bot_address)
                        print_balances(w3, balances_after, "操作后")
                        print("\n🎉 注资完成!")
                    else:
                        print("\n❌ 转移失败")
                        sys.exit(1)
            else:
                print("\n❌ 余额不足，无法继续")
                sys.exit(1)
        else:
            _do_wrap_and_transfer(w3, account, weth_contract, bot_address, balances)
    
    print()


def _do_wrap_and_transfer(w3, account, weth_contract, bot_address, balances):
    """执行包装 + 转移流程"""
    user_eth_eth = float(w3.from_wei(balances["user_eth"], 'ether'))
    
    # 询问包装金额
    wrap_amount_eth = ask_amount(
        "\n   输入包装金额 (ETH)",
        DEFAULT_FUND_AMOUNT_ETH,
        user_eth_eth - 0.001  # 保留一些 gas
    )
    wrap_amount_wei = Web3.to_wei(wrap_amount_eth, 'ether')
    
    print(f"\n📝 即将:")
    print(f"   1. 包装 {wrap_amount_eth:.6f} ETH -> WETH")
    print(f"   2. 转移 {wrap_amount_eth:.6f} WETH 到 {bot_address[:20]}...")
    
    if not ask_user_choice("确认执行?", "y"):
        print("\n⚠️ 用户取消操作")
        sys.exit(0)
    
    # 包装 ETH
    if not wrap_eth(w3, account, weth_contract, wrap_amount_wei):
        print("\n❌ 包装失败，操作中止")
        sys.exit(1)
    
    # 等待余额更新
    if not wait_for_weth_balance(w3, weth_contract, account.address, wrap_amount_wei):
        print("\n❌ WETH 余额未更新，操作中止")
        print("⚠️ 注意: WETH 可能已在你的钱包中，请稍后手动检查并转移")
        sys.exit(1)
    
    # 转移 WETH
    if not transfer_weth(w3, account, weth_contract, bot_address, wrap_amount_wei):
        print("\n❌ 转移失败，操作中止")
        print("⚠️ 注意: WETH 仍在你的钱包中，可以稍后手动转移")
        sys.exit(1)
    
    # 显示结果
    balances_after = get_balances(w3, weth_contract, account.address, bot_address)
    print_balances(w3, balances_after, "操作后")
    
    eth_change = balances_after["user_eth"] - balances["user_eth"]
    weth_change = balances_after["bot_weth"] - balances["bot_weth"]
    print(f"\n📈 余额变化:")
    print(f"   用户 ETH: {w3.from_wei(eth_change, 'ether'):+.6f} ETH (包含 gas 费)")
    print(f"   合约 WETH: {w3.from_wei(weth_change, 'ether'):+.6f} WETH")
    
    print("\n" + "=" * 60)
    print("🎉 注资完成!")
    print("=" * 60)
    print(f"\n📋 摘要:")
    print(f"   合约现有 WETH: {w3.from_wei(balances_after['bot_weth'], 'ether'):.6f} WETH")
    print(f"\n📝 下一步:")
    print(f"   运行主程序: python main.py")


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
