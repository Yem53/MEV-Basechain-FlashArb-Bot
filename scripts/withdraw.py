#!/usr/bin/env python3
"""
FlashBotV3 资金提取脚本

从 FlashBotV3 合约中提取资金（利润/Gas）到 Owner 钱包：
1. 提取 WETH 余额
2. 提取原生 ETH 余额

⚠️ 此脚本会执行真实交易，请仔细确认地址
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account

# 加载环境变量
load_dotenv()


# ============================================================
# 常量配置
# ============================================================

# WETH 合约地址 (Base Mainnet)
WETH_ADDRESS = "0x4200000000000000000000000000000000000006"

# FlashBotV3 合约 ABI (仅提取相关函数)
FLASHBOT_ABI = [
    # withdrawToken(address token, address to, uint256 amount)
    {
        "inputs": [
            {"name": "token", "type": "address"},
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "withdrawToken",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    # withdrawETH(address payable to, uint256 amount)
    {
        "inputs": [
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "withdrawETH",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    # getTokenBalance(address token) view returns (uint256)
    {
        "inputs": [{"name": "token", "type": "address"}],
        "name": "getTokenBalance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    # getETHBalance() view returns (uint256)
    {
        "inputs": [],
        "name": "getETHBalance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    # owner() view returns (address)
    {
        "inputs": [],
        "name": "owner",
        "outputs": [{"name": "", "type": "address"}],
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


def get_raw_transaction(signed_tx):
    """
    兼容 Web3.py 不同版本的 rawTransaction 获取方式
    
    参数:
        signed_tx: 签名后的交易对象
    
    返回:
        raw transaction bytes
    """
    # 尝试 snake_case (新版本)
    if hasattr(signed_tx, 'raw_transaction'):
        return signed_tx.raw_transaction
    # 回退 camelCase (旧版本)
    elif hasattr(signed_tx, 'rawTransaction'):
        return signed_tx.rawTransaction
    else:
        raise AttributeError("无法获取 raw transaction，请检查 Web3.py 版本")


def withdraw_weth(
    w3: Web3,
    account: Account,
    contract,
    to_address: str,
    amount_wei: int
) -> bool:
    """
    从合约提取 WETH
    
    参数:
        w3: Web3 实例
        account: 账户
        contract: FlashBot 合约实例
        to_address: 目标地址
        amount_wei: 金额 (wei), 0 表示全部
    
    返回:
        是否成功
    """
    print(f"\n💰 提取 WETH...")
    print(f"   目标地址: {to_address}")
    print(f"   金额: {w3.from_wei(amount_wei, 'ether'):.6f} WETH {'(全部)' if amount_wei == 0 else ''}")
    
    try:
        weth_address = w3.to_checksum_address(WETH_ADDRESS)
        
        # 获取 nonce
        nonce = w3.eth.get_transaction_count(account.address, 'pending')
        print(f"   Nonce: {nonce}")
        
        # 估算 gas
        gas_estimate = contract.functions.withdrawToken(
            weth_address,
            to_address,
            amount_wei
        ).estimate_gas({"from": account.address})
        print(f"   预估 Gas: {gas_estimate:,}")
        
        # 获取 gas 价格
        gas_price = w3.eth.gas_price
        print(f"   Gas 价格: {w3.from_wei(gas_price, 'gwei'):.4f} Gwei")
        
        # 构建交易
        tx = contract.functions.withdrawToken(
            weth_address,
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
            print(f"   ✅ WETH 提取成功!")
            print(f"   使用 Gas: {receipt['gasUsed']:,}")
            return True
        else:
            print(f"   ❌ WETH 提取失败 (交易回滚)")
            return False
            
    except Exception as e:
        print(f"   ❌ 错误: {e}")
        return False


def withdraw_eth(
    w3: Web3,
    account: Account,
    contract,
    to_address: str,
    amount_wei: int
) -> bool:
    """
    从合约提取原生 ETH
    
    参数:
        w3: Web3 实例
        account: 账户
        contract: FlashBot 合约实例
        to_address: 目标地址
        amount_wei: 金额 (wei), 0 表示全部
    
    返回:
        是否成功
    """
    print(f"\n⛽ 提取原生 ETH...")
    print(f"   目标地址: {to_address}")
    print(f"   金额: {w3.from_wei(amount_wei, 'ether'):.6f} ETH {'(全部)' if amount_wei == 0 else ''}")
    
    try:
        # 获取 nonce
        nonce = w3.eth.get_transaction_count(account.address, 'pending')
        print(f"   Nonce: {nonce}")
        
        # 估算 gas
        gas_estimate = contract.functions.withdrawETH(
            to_address,
            amount_wei
        ).estimate_gas({"from": account.address})
        print(f"   预估 Gas: {gas_estimate:,}")
        
        # 获取 gas 价格
        gas_price = w3.eth.gas_price
        print(f"   Gas 价格: {w3.from_wei(gas_price, 'gwei'):.4f} Gwei")
        
        # 构建交易
        tx = contract.functions.withdrawETH(
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
            print(f"   ✅ ETH 提取成功!")
            print(f"   使用 Gas: {receipt['gasUsed']:,}")
            return True
        else:
            print(f"   ❌ ETH 提取失败 (交易回滚)")
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
    print("💸 FlashBotV3 资金提取脚本")
    print("=" * 60)
    
    # ===== 1. 加载配置 =====
    flashbot_address = os.getenv("FLASHBOT_ADDRESS")
    if not flashbot_address:
        print("❌ 错误: 请在 .env 中设置 FLASHBOT_ADDRESS")
        sys.exit(1)
    
    # ===== 2. 连接网络 =====
    print()
    w3, account = connect_web3()
    
    # ===== 3. 安全检查 - 显示关键地址 =====
    flashbot_address = w3.to_checksum_address(flashbot_address)
    
    print("\n" + "=" * 60)
    print("⚠️  安全检查 - 请确认以下地址正确")
    print("=" * 60)
    print(f"   👤 Owner 地址 (你的钱包): {account.address}")
    print(f"   🤖 合约地址 (FlashBotV3): {flashbot_address}")
    print("=" * 60)
    
    # ===== 4. 初始化合约 =====
    contract = w3.eth.contract(address=flashbot_address, abi=FLASHBOT_ABI)
    
    # ===== 5. 验证 Owner 权限 =====
    try:
        contract_owner = contract.functions.owner().call()
        if contract_owner.lower() != account.address.lower():
            print(f"\n❌ 错误: 你不是合约 Owner!")
            print(f"   合约 Owner: {contract_owner}")
            print(f"   你的地址:   {account.address}")
            sys.exit(1)
        print(f"\n✅ Owner 验证通过")
    except Exception as e:
        print(f"\n⚠️ 警告: 无法验证 Owner ({e})")
    
    # ===== 6. 查询余额 =====
    weth_address = w3.to_checksum_address(WETH_ADDRESS)
    
    try:
        weth_balance = contract.functions.getTokenBalance(weth_address).call()
    except:
        # 如果合约没有 getTokenBalance，使用 ERC20 balanceOf
        from web3 import Web3
        erc20_abi = [{"constant": True, "inputs": [{"name": "", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"}]
        weth_contract = w3.eth.contract(address=weth_address, abi=erc20_abi)
        weth_balance = weth_contract.functions.balanceOf(flashbot_address).call()
    
    try:
        eth_balance = contract.functions.getETHBalance().call()
    except:
        # 如果合约没有 getETHBalance，直接查询
        eth_balance = w3.eth.get_balance(flashbot_address)
    
    print(f"\n📊 合约余额:")
    print(f"   WETH: {w3.from_wei(weth_balance, 'ether'):.6f} WETH")
    print(f"   ETH:  {w3.from_wei(eth_balance, 'ether'):.6f} ETH")
    
    # ===== 7. 检查是否有余额可提取 =====
    if weth_balance == 0 and eth_balance == 0:
        print("\n📭 合约中没有可提取的资金")
        print("=" * 60)
        sys.exit(0)
    
    # ===== 8. 用户确认 =====
    print("\n" + "-" * 60)
    print("📝 即将执行以下操作:")
    if weth_balance > 0:
        print(f"   • 提取 {w3.from_wei(weth_balance, 'ether'):.6f} WETH")
    if eth_balance > 0:
        print(f"   • 提取 {w3.from_wei(eth_balance, 'ether'):.6f} ETH")
    print(f"   目标地址: {account.address}")
    print("-" * 60)
    
    confirm = input("\n确认执行提取? (输入 'yes' 继续): ").strip().lower()
    if confirm != 'yes':
        print("\n❌ 用户取消操作")
        sys.exit(0)
    
    # ===== 9. 执行提取 =====
    success_count = 0
    total_count = 0
    
    # 提取 WETH
    if weth_balance > 0:
        total_count += 1
        if withdraw_weth(w3, account, contract, account.address, 0):
            success_count += 1
    
    # 提取 ETH
    if eth_balance > 0:
        total_count += 1
        if withdraw_eth(w3, account, contract, account.address, 0):
            success_count += 1
    
    # ===== 10. 显示结果 =====
    print("\n" + "=" * 60)
    if success_count == total_count:
        print("🎉 所有提取操作完成!")
    else:
        print(f"⚠️ 提取完成: {success_count}/{total_count} 成功")
    print("=" * 60)
    
    # 显示最终余额
    try:
        final_weth = contract.functions.getTokenBalance(weth_address).call()
        final_eth = contract.functions.getETHBalance().call()
        print(f"\n📊 合约剩余余额:")
        print(f"   WETH: {w3.from_wei(final_weth, 'ether'):.6f} WETH")
        print(f"   ETH:  {w3.from_wei(final_eth, 'ether'):.6f} ETH")
    except:
        pass
    
    # 显示钱包余额
    wallet_eth = w3.eth.get_balance(account.address)
    print(f"\n👛 你的钱包余额:")
    print(f"   ETH: {w3.from_wei(wallet_eth, 'ether'):.6f} ETH")
    
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

