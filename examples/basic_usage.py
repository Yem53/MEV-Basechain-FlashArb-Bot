"""
FlashArb-Core: 基础使用示例

此示例演示如何使用核心基础架构:
1. 加载链配置
2. 连接到区块链网络
3. 查询区块链数据
4. 处理 Gas 定价
"""

import asyncio
import logging

# 配置示例的日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# 导入 FlashArb-Core 模块
from core.config_loader import ConfigLoader, load_chain_config
from core.network import NetworkManager, create_network_manager
from utils.abi_loader import load_abi, get_erc20_abi


async def example_basic_connection():
    """示例: 基础连接和区块查询"""
    print("\n" + "=" * 60)
    print("示例 1: 基础连接")
    print("=" * 60)
    
    # 加载 BASE 链配置
    config = load_chain_config("BASE")
    print(f"链: {config.name}")
    print(f"链 ID: {config.chain_id}")
    print(f"原生代币: {config.native_token}")
    print(f"RPC URLs: 配置了 {len(config.rpc_urls)} 个端点")
    
    # 使用异步上下文管理器连接（推荐方式）
    async with NetworkManager(config) as network:
        # 获取最新区块
        block_number = await network.get_block_number()
        print(f"最新区块: {block_number}")
        
        # 获取区块详情
        block = await network.get_latest_block()
        print(f"区块哈希: {block['hash'].hex()}")
        print(f"区块时间戳: {block['timestamp']}")
        print(f"交易数: {len(block['transactions'])}")


async def example_gas_pricing():
    """示例: 不同链类型的 Gas 价格计算"""
    print("\n" + "=" * 60)
    print("示例 2: Gas 定价")
    print("=" * 60)
    
    # 测试 EIP-1559 链（BASE）
    base_config = load_chain_config("BASE")
    async with NetworkManager(base_config) as network:
        gas_params = await network.get_gas_params(speed="fast")
        
        print(f"\n{base_config.name} (EIP-1559):")
        print(f"  Max Fee Per Gas: {gas_params.max_fee_per_gas / 1e9:.4f} Gwei")
        print(f"  Max Priority Fee: {gas_params.max_priority_fee_per_gas / 1e9:.4f} Gwei")
    
    # 测试 Legacy 链（BSC）
    bsc_config = load_chain_config("BSC")
    async with NetworkManager(bsc_config) as network:
        gas_params = await network.get_gas_params(speed="fast")
        
        print(f"\n{bsc_config.name} (Legacy):")
        print(f"  Gas Price: {gas_params.gas_price / 1e9:.4f} Gwei")


async def example_multi_chain():
    """示例: 并发查询多条链"""
    print("\n" + "=" * 60)
    print("示例 3: 多链并发查询")
    print("=" * 60)
    
    loader = ConfigLoader()
    chains = ["BASE", "ARBITRUM", "POLYGON"]
    
    async def query_chain(chain_name: str):
        config = loader.get_chain_config(chain_name)
        async with NetworkManager(config) as network:
            block = await network.get_block_number()
            latency = await network.ping()
            return chain_name, block, latency
    
    # 并发查询所有链
    tasks = [query_chain(chain) for chain in chains]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    
    print("\n结果:")
    for result in results:
        if isinstance(result, Exception):
            print(f"  错误: {result}")
        else:
            chain, block, latency = result
            print(f"  {chain}: 区块 #{block}, 延迟: {latency:.2f}ms")


async def example_rpc_health():
    """示例: 监控 RPC 健康状态和故障转移"""
    print("\n" + "=" * 60)
    print("示例 4: RPC 健康监控")
    print("=" * 60)
    
    config = load_chain_config("ETHEREUM")
    
    async with NetworkManager(config) as network:
        # 发送多个请求以收集健康数据
        for _ in range(5):
            await network.get_block_number()
        
        # 检查 RPC 健康指标
        health = network.get_rpc_health()
        
        print("\nRPC 健康指标:")
        for url, metrics in health.items():
            print(f"\n  URL: {url[:50]}...")
            print(f"    健康: {metrics.is_healthy}")
            print(f"    请求数: {metrics.total_requests}")
            print(f"    平均延迟: {metrics.avg_latency_ms:.2f}ms")
            print(f"    连续失败: {metrics.consecutive_failures}")
        
        # 找到最快的 RPC
        fastest = network.get_fastest_rpc()
        if fastest:
            print(f"\n最快的 RPC: {fastest[:50]}...")


async def example_abi_loading():
    """示例: 加载和使用合约 ABI"""
    print("\n" + "=" * 60)
    print("示例 5: ABI 加载")
    print("=" * 60)
    
    # 加载 ERC20 ABI
    erc20_abi = get_erc20_abi()
    print(f"已加载 ERC20 ABI: {len(erc20_abi)} 个条目")
    
    # 列出可用函数
    functions = [
        entry["name"] for entry in erc20_abi 
        if entry.get("type") == "function"
    ]
    print(f"函数: {', '.join(functions)}")
    
    # 加载 Uniswap V2 路由器 ABI
    router_abi = load_abi("uniswap_v2_router")
    print(f"\n已加载 Uniswap V2 Router ABI: {len(router_abi)} 个条目")
    
    # 列出 swap 函数
    swap_functions = [
        entry["name"] for entry in router_abi 
        if entry.get("type") == "function" and "swap" in entry["name"].lower()
    ]
    print(f"Swap 函数: {', '.join(swap_functions)}")


async def example_contract_interaction():
    """示例: 从智能合约读取数据"""
    print("\n" + "=" * 60)
    print("示例 6: 合约交互")
    print("=" * 60)
    
    config = load_chain_config("BASE")
    
    async with NetworkManager(config) as network:
        # Base 上的 WETH 合约
        weth_address = config.wnative_address
        
        # 使用 ERC20 ABI 创建合约实例
        erc20_abi = get_erc20_abi()
        contract = network.w3.eth.contract(
            address=network.w3.to_checksum_address(weth_address),
            abi=erc20_abi,
        )
        
        # 读取代币信息
        name = await contract.functions.name().call()
        symbol = await contract.functions.symbol().call()
        decimals = await contract.functions.decimals().call()
        total_supply = await contract.functions.totalSupply().call()
        
        print(f"\n{config.name} 上的 WETH 合约:")
        print(f"  地址: {weth_address}")
        print(f"  名称: {name}")
        print(f"  符号: {symbol}")
        print(f"  小数位: {decimals}")
        print(f"  总供应量: {total_supply / 10**decimals:,.2f} {symbol}")


async def main():
    """运行所有示例"""
    print("\n" + "=" * 60)
    print("FlashArb-Core: 基础架构示例")
    print("=" * 60)
    
    try:
        await example_basic_connection()
        await example_gas_pricing()
        await example_multi_chain()
        await example_rpc_health()
        await example_abi_loading()
        await example_contract_interaction()
        
        print("\n" + "=" * 60)
        print("所有示例运行成功!")
        print("=" * 60)
        
    except Exception as e:
        print(f"\n运行示例时出错: {e}")
        raise


if __name__ == "__main__":
    asyncio.run(main())
