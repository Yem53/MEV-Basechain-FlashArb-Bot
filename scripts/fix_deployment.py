#!/usr/bin/env python3
"""
FlashBot 部署恢复脚本

用于修复部署中断后的状态：
1. 重新编译合约获取 ABI
2. 连接到已部署的合约
3. 执行缺失的 approveRouter 调用
4. 保存 deployments.json

⚠️ 此脚本不会重新部署合约，只会修复状态
"""

import os
import sys
import json
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional

from dotenv import load_dotenv
from web3 import Web3
from eth_account import Account
import solcx

# 加载环境变量
load_dotenv()


# ============================================================
# 常量配置
# ============================================================

# 已部署的合约地址 (不要修改!)
EXISTING_CONTRACT_ADDRESS = "0xA4099ADD722ca77c958220171FAa6C9C07674596"

# Solidity 编译器版本
SOLC_VERSION = "0.8.19"

# Base Mainnet 配置
BASE_WETH = "0x4200000000000000000000000000000000000006"
BASE_BASESWAP_ROUTER = "0x29f216eF31E127117E3B2902A2462A772242095C"

# 需要授权的路由器列表 (可扩展)
ROUTERS_TO_APPROVE = [
    ("BaseSwap", BASE_BASESWAP_ROUTER),
    # 可以添加更多路由器:
    # ("SushiSwap", "0x6BDED42c6DA8FBf0d2bA55B2fa120C5e0c8D7891"),
    # ("Aerodrome", "0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43"),
]


# ============================================================
# 辅助函数
# ============================================================

def get_project_root() -> Path:
    """获取项目根目录"""
    return Path(__file__).parent.parent


def compile_contract() -> Dict[str, Any]:
    """
    编译 FlashBot.sol 合约以获取 ABI
    
    返回:
        包含 abi 和 bytecode 的字典
    """
    project_root = get_project_root()
    contracts_dir = project_root / "contracts"
    
    # 确保编译器已安装
    print(f"📦 检查 Solidity 编译器 v{SOLC_VERSION}...")
    
    installed_versions = [str(v) for v in solcx.get_installed_solc_versions()]
    if SOLC_VERSION not in installed_versions:
        print(f"   正在安装 solc v{SOLC_VERSION}...")
        solcx.install_solc(SOLC_VERSION)
    
    solcx.set_solc_version(SOLC_VERSION)
    print(f"   ✅ solc v{SOLC_VERSION} 已就绪")
    
    # 读取合约源码
    main_contract = contracts_dir / "FlashBot.sol"
    interfaces_dir = contracts_dir / "interfaces"
    libraries_dir = contracts_dir / "libraries"
    
    # 构建源码映射
    sources = {}
    
    # 主合约
    sources["FlashBot.sol"] = {
        "content": main_contract.read_text(encoding="utf-8")
    }
    
    # 接口
    for interface_file in interfaces_dir.glob("*.sol"):
        key = f"interfaces/{interface_file.name}"
        sources[key] = {
            "content": interface_file.read_text(encoding="utf-8")
        }
    
    # 库
    for lib_file in libraries_dir.glob("*.sol"):
        key = f"libraries/{lib_file.name}"
        sources[key] = {
            "content": lib_file.read_text(encoding="utf-8")
        }
    
    # 编译设置
    compile_input = {
        "language": "Solidity",
        "sources": sources,
        "settings": {
            "optimizer": {
                "enabled": True,
                "runs": 200
            },
            "outputSelection": {
                "*": {
                    "*": ["abi", "evm.bytecode.object"]
                }
            }
        }
    }
    
    print("🔨 编译合约...")
    
    compiled = solcx.compile_standard(
        compile_input,
        allow_paths=[str(contracts_dir)]
    )
    
    # 检查错误和警告
    if "errors" in compiled:
        for error in compiled["errors"]:
            severity = error.get("severity", "unknown")
            message = error.get("formattedMessage", error.get("message", "Unknown error"))
            
            if severity == "error":
                print(f"   ❌ 编译错误: {message}")
                raise Exception("编译失败")
            elif severity == "warning":
                # 忽略警告，只打印简短信息
                short_msg = message.split('\n')[0] if '\n' in message else message
                print(f"   ⚠️ 警告: {short_msg[:50]}...")
    
    # 提取合约数据
    contract_data = compiled["contracts"]["FlashBot.sol"]["FlashBot"]
    abi = contract_data["abi"]
    bytecode = contract_data["evm"]["bytecode"]["object"]
    
    print(f"   ✅ 编译成功")
    print(f"   ABI 函数数量: {len(abi)}")
    
    return {
        "abi": abi,
        "bytecode": bytecode
    }


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
    balance = w3.eth.get_balance(account.address)
    
    print(f"\n👛 账户信息:")
    print(f"   地址: {account.address}")
    print(f"   余额: {w3.from_wei(balance, 'ether'):.6f} ETH")
    
    return w3, account


def approve_router(
    w3: Web3,
    account: Account,
    contract,
    token_address: str,
    router_address: str,
    router_name: str
) -> bool:
    """
    调用合约的 approveRouter 函数
    
    参数:
        w3: Web3 实例
        account: 账户
        contract: 合约实例
        token_address: 代币地址
        router_address: 路由器地址
        router_name: 路由器名称 (用于日志)
    
    返回:
        是否成功
    """
    token_address = w3.to_checksum_address(token_address)
    router_address = w3.to_checksum_address(router_address)
    
    print(f"\n🔓 授权 {router_name} 路由器...")
    print(f"   代币: {token_address}")
    print(f"   路由器: {router_address}")
    
    try:
        # 获取当前 nonce (使用 'pending' 确保获取最新值)
        nonce = w3.eth.get_transaction_count(account.address, 'pending')
        print(f"   当前 nonce: {nonce}")
        
        # 估算 gas
        gas_estimate = contract.functions.approveRouter(
            token_address,
            router_address
        ).estimate_gas({"from": account.address})
        
        print(f"   预估 Gas: {gas_estimate:,}")
        
        # 获取 gas 价格
        gas_price = w3.eth.gas_price
        print(f"   Gas 价格: {w3.from_wei(gas_price, 'gwei'):.4f} Gwei")
        
        # 构建交易
        tx = contract.functions.approveRouter(
            token_address,
            router_address
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
            print(f"   ✅ 授权成功!")
            print(f"   使用 Gas: {receipt['gasUsed']:,}")
            return True
        else:
            print(f"   ❌ 授权失败 (交易回滚)")
            return False
            
    except Exception as e:
        print(f"   ❌ 错误: {e}")
        return False


def save_deployment(
    contract_address: str,
    abi: list,
    chain_id: int,
    deployer: str
) -> None:
    """
    保存部署信息到 deployments.json
    
    参数:
        contract_address: 合约地址
        abi: 合约 ABI
        chain_id: 链 ID
        deployer: 部署者地址
    """
    project_root = get_project_root()
    deployments_file = project_root / "deployments.json"
    
    # 确定网络名称
    network_names = {
        1: "ethereum_mainnet",
        8453: "base_mainnet",
        84531: "base_goerli",
        84532: "base_sepolia",
        31337: "anvil_local",
    }
    network_name = network_names.get(chain_id, f"chain_{chain_id}")
    
    # 加载现有数据或创建新的
    if deployments_file.exists():
        try:
            with open(deployments_file, "r", encoding="utf-8") as f:
                deployments = json.load(f)
        except json.JSONDecodeError:
            deployments = {}
    else:
        deployments = {}
    
    # 更新部署信息
    deployments[network_name] = {
        "contract_address": contract_address,
        "abi": abi,
        "chain_id": chain_id,
        "deployer": deployer,
        "deployed_at": datetime.now().isoformat(),
        "recovered": True,  # 标记为恢复的部署
        "recovery_note": "Deployment recovered via fix_deployment.py"
    }
    
    # 保存
    with open(deployments_file, "w", encoding="utf-8") as f:
        json.dump(deployments, f, indent=2, ensure_ascii=False)
    
    print(f"\n💾 部署信息已保存到 {deployments_file.name}")
    print(f"   网络: {network_name}")
    print(f"   合约地址: {contract_address}")


def verify_contract_owner(w3: Web3, contract, expected_owner: str) -> bool:
    """
    验证合约所有者
    
    参数:
        w3: Web3 实例
        contract: 合约实例
        expected_owner: 预期的所有者地址
    
    返回:
        所有者是否匹配
    """
    try:
        owner = contract.functions.owner().call()
        owner = w3.to_checksum_address(owner)
        expected = w3.to_checksum_address(expected_owner)
        
        if owner == expected:
            print(f"   ✅ 所有者验证通过: {owner}")
            return True
        else:
            print(f"   ❌ 所有者不匹配!")
            print(f"      合约所有者: {owner}")
            print(f"      你的地址: {expected}")
            return False
    except Exception as e:
        print(f"   ⚠️ 无法验证所有者: {e}")
        return False


# ============================================================
# 主函数
# ============================================================

def main():
    """主函数"""
    print("=" * 60)
    print("🔧 FlashBot 部署恢复脚本")
    print("=" * 60)
    print(f"\n📋 目标合约: {EXISTING_CONTRACT_ADDRESS}")
    print()
    
    # ===== 1. 编译合约获取 ABI =====
    compiled = compile_contract()
    abi = compiled["abi"]
    
    # ===== 2. 连接网络 =====
    print()
    w3, account = connect_web3()
    chain_id = w3.eth.chain_id
    
    # ===== 3. 连接到现有合约 =====
    print(f"\n📄 连接到现有合约...")
    contract_address = w3.to_checksum_address(EXISTING_CONTRACT_ADDRESS)
    contract = w3.eth.contract(address=contract_address, abi=abi)
    print(f"   地址: {contract_address}")
    
    # 验证合约所有者
    print(f"\n🔐 验证合约所有权...")
    if not verify_contract_owner(w3, contract, account.address):
        print("\n❌ 你不是此合约的所有者，无法执行授权操作!")
        sys.exit(1)
    
    # ===== 4. 执行授权 =====
    weth_address = os.getenv("WETH_ADDRESS", BASE_WETH)
    
    print(f"\n📋 授权配置:")
    print(f"   WETH: {weth_address}")
    print(f"   路由器数量: {len(ROUTERS_TO_APPROVE)}")
    
    success_count = 0
    for router_name, router_address in ROUTERS_TO_APPROVE:
        result = approve_router(
            w3, account,
            contract,
            weth_address,
            router_address,
            router_name
        )
        if result:
            success_count += 1
    
    # ===== 5. 保存部署信息 =====
    save_deployment(
        contract_address=contract_address,
        abi=abi,
        chain_id=chain_id,
        deployer=account.address
    )
    
    # ===== 完成 =====
    print("\n" + "=" * 60)
    print("🎉 恢复完成!")
    print("=" * 60)
    print(f"\n📋 摘要:")
    print(f"   合约地址: {contract_address}")
    print(f"   链 ID: {chain_id}")
    print(f"   授权成功: {success_count}/{len(ROUTERS_TO_APPROVE)}")
    print(f"\n📝 下一步:")
    print(f"   1. 更新 .env 中的 FLASHBOT_ADDRESS={contract_address}")
    print(f"   2. 运行主程序: python main.py")
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

