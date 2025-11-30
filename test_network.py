"""
FlashArb-Core 网络管理器测试脚本

此脚本验证以下功能:
1. 配置加载
2. RPC 故障转移逻辑（使用假的 RPC URL）
3. 获取区块号
4. 获取 Gas 价格

运行: python test_network.py
"""

import asyncio
import logging
import sys
from copy import deepcopy

# 配置日志 - 显示详细信息以观察故障转移
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# 导入核心模块
from core.config_loader import ConfigLoader
from core.network import NetworkManager, NetworkState


async def test_failover_and_queries():
    """
    测试 RPC 故障转移和基本查询功能
    
    步骤:
    1. 加载 BASE 链配置
    2. 在 RPC 列表最前面插入一个假的 URL
    3. 初始化 NetworkManager（应该故障转移到真实的 RPC）
    4. 打印当前区块号和 Gas 价格
    """
    print("\n" + "=" * 70)
    print("🧪 FlashArb-Core 网络管理器测试")
    print("=" * 70)
    
    # =========================================================
    # 步骤 1: 加载配置
    # =========================================================
    print("\n📋 步骤 1: 加载 BASE 链配置...")
    
    loader = ConfigLoader()
    config = loader.get_chain_config("BASE")
    
    print(f"   链名称: {config.name}")
    print(f"   链 ID: {config.chain_id}")
    print(f"   原生代币: {config.native_token}")
    print(f"   Gas 类型: {config.gas_config.type}")
    print(f"   原始 RPC 数量: {len(config.rpc_urls)}")
    
    # =========================================================
    # 步骤 2: 插入假的 RPC URL 来测试故障转移
    # =========================================================
    print("\n🔧 步骤 2: 插入假的 RPC URL 测试故障转移...")
    
    # 创建一个假的 RPC URL
    fake_rpc_url = "https://fake-rpc-that-does-not-exist.invalid"
    
    # 在列表最前面插入假的 URL
    original_rpcs = config.rpc_urls.copy()
    config.rpc_urls = [fake_rpc_url] + original_rpcs
    
    print(f"   假 RPC URL: {fake_rpc_url}")
    print(f"   修改后 RPC 列表:")
    for i, url in enumerate(config.rpc_urls):
        marker = "❌ (假的)" if url == fake_rpc_url else "✓"
        print(f"      [{i}] {marker} {url[:50]}...")
    
    # =========================================================
    # 步骤 3: 初始化 NetworkManager 并观察故障转移
    # =========================================================
    print("\n🌐 步骤 3: 初始化 NetworkManager...")
    print("   (预期: 第一个假 RPC 会失败，然后故障转移到真实 RPC)")
    print("-" * 70)
    
    try:
        async with NetworkManager(config) as network:
            # 检查连接状态
            print("-" * 70)
            print(f"\n✅ 连接成功!")
            print(f"   当前状态: {network.state.value}")
            print(f"   当前 RPC: {network.current_rpc_url[:50]}...")
            
            # =========================================================
            # 步骤 4: 获取区块号
            # =========================================================
            print("\n📦 步骤 4: 获取当前区块号...")
            
            block_number = await network.get_block_number()
            print(f"   当前区块号: {block_number:,}")
            
            # =========================================================
            # 步骤 5: 获取 Gas 参数
            # =========================================================
            print("\n⛽ 步骤 5: 获取 Gas 参数...")
            
            gas_params = await network.get_gas_params(speed="fast")
            
            if gas_params.is_eip1559:
                print(f"   Gas 模式: EIP-1559")
                print(f"   Max Fee Per Gas: {gas_params.max_fee_per_gas / 1e9:.4f} Gwei")
                print(f"   Max Priority Fee: {gas_params.max_priority_fee_per_gas / 1e9:.4f} Gwei")
            else:
                print(f"   Gas 模式: Legacy")
                print(f"   Gas Price: {gas_params.gas_price / 1e9:.4f} Gwei")
            
            # =========================================================
            # 步骤 6: 检查 RPC 健康状态
            # =========================================================
            print("\n📊 步骤 6: RPC 健康状态...")
            
            health = network.get_rpc_health()
            for url, metrics in health.items():
                status = "✅ 健康" if metrics.is_healthy else "❌ 不健康"
                is_fake = "（假的）" if url == fake_rpc_url else ""
                print(f"\n   {url[:40]}... {is_fake}")
                print(f"      状态: {status}")
                print(f"      请求数: {metrics.total_requests}")
                print(f"      连续失败: {metrics.consecutive_failures}")
                if metrics.avg_latency_ms > 0:
                    print(f"      平均延迟: {metrics.avg_latency_ms:.2f}ms")
            
            # =========================================================
            # 测量延迟
            # =========================================================
            print("\n⏱️  测量 RPC 延迟...")
            latency = await network.ping()
            print(f"   Ping 延迟: {latency:.2f}ms")
            
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False
    
    # =========================================================
    # 测试完成
    # =========================================================
    print("\n" + "=" * 70)
    print("🎉 所有测试通过!")
    print("=" * 70)
    print("\n✅ 故障转移逻辑验证成功:")
    print("   - 假的 RPC URL 被正确跳过")
    print("   - 自动切换到可用的 RPC 节点")
    print("   - 区块号和 Gas 价格获取成功")
    print("   - 健康监控正常工作")
    
    return True


async def main():
    """主入口函数"""
    success = await test_failover_and_queries()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())

