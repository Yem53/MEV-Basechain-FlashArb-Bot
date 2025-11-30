#!/usr/bin/env python3
"""
FlashBot 合约部署脚本

功能：
1. 编译 Solidity 合约
2. 部署到指定网络
3. 预授权路由器（无限授权）
4. 保存部署信息到 deployments.json

使用方法：
    python scripts/deploy.py

环境变量（在 .env 文件中设置）：
    PRIVATE_KEY: 部署账户私钥
    RPC_URL: 网络 RPC 端点
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

# 添加项目根目录到路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from web3 import Web3

# 加载环境变量
load_dotenv(PROJECT_ROOT / ".env")


# ============================================
# 网络配置（从环境变量加载）
# ============================================

def get_network_config() -> dict:
    """
    从环境变量加载网络配置
    
    返回:
        网络配置字典
    """
    return {
        "name": os.getenv("NETWORK_NAME", "Base"),
        "chain_id": int(os.getenv("CHAIN_ID", "8453")),
        "weth": os.getenv("WETH_ADDRESS", "0x4200000000000000000000000000000000000006"),
        "target_router": os.getenv("TARGET_ROUTER", "0x4752ba5DBc23f44D87826276BF6Fd6b1C372aD24"),
    }


# 延迟加载配置（在 main 中调用）
NETWORK_CONFIG = None

# 部署信息保存路径
DEPLOYMENTS_FILE = PROJECT_ROOT / "deployments.json"


# ============================================
# Solidity 编译器
# ============================================

def install_solc(version: str = "0.8.19") -> None:
    """
    安装指定版本的 Solidity 编译器
    
    参数:
        version: Solidity 版本号
    """
    import solcx
    
    print(f"📦 检查 Solidity 编译器 v{version}...")
    
    installed_versions = solcx.get_installed_solc_versions()
    target_version = solcx.install.Version(version)
    
    if target_version not in installed_versions:
        print(f"   正在安装 solc v{version}...")
        solcx.install_solc(version)
        print(f"   ✅ solc v{version} 安装完成")
    else:
        print(f"   ✅ solc v{version} 已安装")
    
    solcx.set_solc_version(version)


def compile_contract() -> Dict[str, Any]:
    """
    编译 FlashBot 合约
    
    返回:
        包含 abi 和 bytecode 的字典
    """
    import solcx
    
    print("🔨 编译合约...")
    
    # 合约文件路径
    contracts_dir = PROJECT_ROOT / "contracts"
    main_contract = contracts_dir / "FlashBot.sol"
    
    if not main_contract.exists():
        raise FileNotFoundError(f"合约文件不存在: {main_contract}")
    
    # 读取所有源文件
    sources = {}
    
    # 主合约
    sources["FlashBot.sol"] = {
        "content": main_contract.read_text(encoding="utf-8")
    }
    
    # 接口文件
    interfaces_dir = contracts_dir / "interfaces"
    if interfaces_dir.exists():
        for sol_file in interfaces_dir.glob("*.sol"):
            rel_path = f"interfaces/{sol_file.name}"
            sources[rel_path] = {
                "content": sol_file.read_text(encoding="utf-8")
            }
    
    # 库文件
    libraries_dir = contracts_dir / "libraries"
    if libraries_dir.exists():
        for sol_file in libraries_dir.glob("*.sol"):
            rel_path = f"libraries/{sol_file.name}"
            sources[rel_path] = {
                "content": sol_file.read_text(encoding="utf-8")
            }
    
    # 编译设置
    compiler_input = {
        "language": "Solidity",
        "sources": sources,
        "settings": {
            "optimizer": {
                "enabled": True,
                "runs": 10000
            },
            "outputSelection": {
                "*": {
                    "*": ["abi", "evm.bytecode.object"]
                }
            }
        }
    }
    
    # 编译
    output = solcx.compile_standard(
        compiler_input,
        allow_paths=[str(contracts_dir)]
    )
    
    # 检查编译错误
    if "errors" in output:
        for error in output["errors"]:
            if error["severity"] == "error":
                raise Exception(f"编译错误: {error['message']}")
            else:
                print(f"   ⚠️ 警告: {error['message']}")
    
    # 提取 FlashBot 合约
    contract_data = output["contracts"]["FlashBot.sol"]["FlashBot"]
    
    abi = contract_data["abi"]
    bytecode = contract_data["evm"]["bytecode"]["object"]
    
    print(f"   ✅ 编译成功")
    print(f"   ABI 函数数量: {len([x for x in abi if x.get('type') == 'function'])}")
    print(f"   Bytecode 大小: {len(bytecode) // 2} bytes")
    
    return {
        "abi": abi,
        "bytecode": bytecode
    }


# ============================================
# 部署函数
# ============================================

def deploy_contract(
    w3: Web3,
    account: Any,
    abi: list,
    bytecode: str
) -> str:
    """
    部署合约
    
    参数:
        w3: Web3 实例
        account: 账户对象
        abi: 合约 ABI
        bytecode: 合约字节码
        
    返回:
        部署的合约地址
    """
    print("🚀 部署合约...")
    
    # 创建合约对象
    Contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    
    # 估算 gas
    gas_estimate = Contract.constructor().estimate_gas({
        "from": account.address
    })
    print(f"   预估 Gas: {gas_estimate:,}")
    
    # 获取 gas 价格
    gas_price = w3.eth.gas_price
    print(f"   Gas 价格: {w3.from_wei(gas_price, 'gwei'):.4f} Gwei")
    
    # 构建部署交易
    tx = Contract.constructor().build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "gas": int(gas_estimate * 1.2),  # 增加 20% 余量
        "gasPrice": gas_price,
    })
    
    # 签名并发送
    signed_tx = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
    
    print(f"   交易哈希: {tx_hash.hex()}")
    print("   等待确认...")
    
    # 等待交易确认
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    
    if receipt["status"] == 1:
        contract_address = receipt["contractAddress"]
        print(f"   ✅ 部署成功!")
        print(f"   合约地址: {contract_address}")
        print(f"   使用 Gas: {receipt['gasUsed']:,}")
        return contract_address
    else:
        raise Exception("合约部署失败（交易 revert）")


def approve_router(
    w3: Web3,
    account: Any,
    contract_address: str,
    abi: list,
    token_address: str,
    router_address: str
) -> bool:
    """
    预授权路由器使用代币（无限授权）
    
    参数:
        w3: Web3 实例
        account: 账户对象
        contract_address: FlashBot 合约地址
        abi: 合约 ABI
        token_address: 代币地址
        router_address: 路由器地址
        
    返回:
        是否成功
    """
    # 转换为 checksum 地址
    token_address = w3.to_checksum_address(token_address)
    router_address = w3.to_checksum_address(router_address)
    
    print(f"🔓 预授权路由器...")
    print(f"   代币: {token_address}")
    print(f"   路由器: {router_address}")
    
    # 创建合约实例
    contract = w3.eth.contract(address=contract_address, abi=abi)
    
    # 估算 gas
    gas_estimate = contract.functions.approveRouter(
        token_address,
        router_address
    ).estimate_gas({"from": account.address})
    
    # 构建交易 - 使用 'pending' 获取最新 nonce（包括待确认交易）
    nonce = w3.eth.get_transaction_count(account.address, 'pending')
    print(f"   当前 nonce: {nonce}")
    
    tx = contract.functions.approveRouter(
        token_address,
        router_address
    ).build_transaction({
        "from": account.address,
        "nonce": nonce,
        "gas": int(gas_estimate * 1.2),
        "gasPrice": w3.eth.gas_price,
    })
    
    # 签名并发送
    signed_tx = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed_tx.rawTransaction)
    
    print(f"   交易哈希: {tx_hash.hex()}")
    
    # 等待确认
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
    
    if receipt["status"] == 1:
        print(f"   ✅ 授权成功!")
        return True
    else:
        print(f"   ❌ 授权失败")
        return False


def save_deployment(
    contract_address: str,
    abi: list,
    network_name: str,
    chain_id: int,
    deployer: str,
    tx_hash: str = ""
) -> None:
    """
    保存部署信息到 JSON 文件
    
    参数:
        contract_address: 合约地址
        abi: 合约 ABI
        network_name: 网络名称
        chain_id: 链 ID
        deployer: 部署者地址
        tx_hash: 部署交易哈希
    """
    import datetime
    
    deployment_info = {
        "contract_address": contract_address,
        "network": network_name,
        "chain_id": chain_id,
        "deployer": deployer,
        "deployed_at": datetime.datetime.now().isoformat(),
        "tx_hash": tx_hash,
        "abi": abi
    }
    
    # 读取现有部署信息
    deployments = {}
    if DEPLOYMENTS_FILE.exists():
        try:
            deployments = json.loads(DEPLOYMENTS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            deployments = {}
    
    # 按链 ID 存储
    deployments[str(chain_id)] = deployment_info
    
    # 保存
    DEPLOYMENTS_FILE.write_text(
        json.dumps(deployments, indent=2, ensure_ascii=False),
        encoding="utf-8"
    )
    
    print(f"💾 部署信息已保存到: {DEPLOYMENTS_FILE}")


# ============================================
# 主函数
# ============================================

def main():
    """主部署流程"""
    
    print("\n" + "=" * 60)
    print("🤖 FlashBot 合约部署脚本")
    print("=" * 60 + "\n")
    
    # ===== 1. 检查环境变量 =====
    private_key = os.getenv("PRIVATE_KEY")
    rpc_url = os.getenv("RPC_URL")
    
    if not private_key:
        print("❌ 错误: 未设置 PRIVATE_KEY 环境变量")
        print("   请在 .env 文件中添加: PRIVATE_KEY=你的私钥")
        sys.exit(1)
    
    if not rpc_url:
        print("❌ 错误: 未设置 RPC_URL 环境变量")
        print("   请在 .env 文件中添加: RPC_URL=https://sepolia.base.org")
        sys.exit(1)
    
    # ===== 2. 连接网络 =====
    print("🌐 连接网络...")
    w3 = Web3(Web3.HTTPProvider(rpc_url))
    
    if not w3.is_connected():
        print("❌ 无法连接到网络")
        sys.exit(1)
    
    chain_id = w3.eth.chain_id
    print(f"   ✅ 已连接")
    print(f"   链 ID: {chain_id}")
    print(f"   RPC: {rpc_url[:50]}...")
    
    # ===== 3. 加载账户 =====
    print("\n👛 加载账户...")
    
    # 确保私钥格式正确
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    
    account = w3.eth.account.from_key(private_key)
    balance = w3.eth.get_balance(account.address)
    
    print(f"   地址: {account.address}")
    print(f"   余额: {w3.from_wei(balance, 'ether'):.6f} ETH")
    
    if balance == 0:
        print("   ⚠️ 警告: 账户余额为 0，无法部署")
        sys.exit(1)
    
    # ===== 4. 安装编译器并编译 =====
    print()
    install_solc("0.8.19")
    
    print()
    compiled = compile_contract()
    
    # ===== 5. 部署合约 =====
    print()
    contract_address = deploy_contract(
        w3, account, 
        compiled["abi"], 
        compiled["bytecode"]
    )
    
    # ===== 6. 加载网络配置并预授权路由器 =====
    print()
    
    # 从环境变量加载网络配置
    network_config = get_network_config()
    weth = network_config["weth"]
    router = network_config["target_router"]
    
    print(f"📋 网络配置:")
    print(f"   WETH: {weth}")
    print(f"   目标路由器: {router}")
    print()
    
    # 授权 WETH
    approve_router(
        w3, account,
        contract_address,
        compiled["abi"],
        weth,
        router
    )
    
    # ===== 7. 保存部署信息 =====
    print()
    save_deployment(
        contract_address=contract_address,
        abi=compiled["abi"],
        network_name=network_config["name"],
        chain_id=chain_id,
        deployer=account.address
    )
    
    # ===== 完成 =====
    print("\n" + "=" * 60)
    print("🎉 部署完成!")
    print("=" * 60)
    print(f"\n📋 部署摘要:")
    print(f"   合约地址: {contract_address}")
    print(f"   网络: {network_config['name']}")
    print(f"   链 ID: {chain_id}")
    print(f"   部署者: {account.address}")
    print(f"\n📝 下一步:")
    print(f"   运行测试: python scripts/test_flash.py")
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


