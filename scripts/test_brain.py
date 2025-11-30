#!/usr/bin/env python3
"""
Phase 3 测试脚本 - 验证扫描器和计算器模块

测试内容：
1. 单元测试（Calculator）：验证 Uniswap V2 AMM 数学公式
2. 集成测试（Multicall）：验证批量获取储备数据功能
3. 逻辑测试（Scanner）：运行套利利润计算模拟

使用方法：
    python scripts/test_brain.py

注意：
    - 需要本地 Anvil fork 运行在 http://127.0.0.1:8545
    - 使用 Base Mainnet 的真实配对地址
"""

import os
import sys
import asyncio
from pathlib import Path
from typing import Tuple, Optional

# 添加项目根目录到路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from web3 import Web3

# 导入我们实现的模块
from core.multicall import Multicall
from core.calculator import (
    get_amount_out,
    get_amount_in,
    get_flash_loan_repayment,
    calculate_arb_profit,
    find_optimal_borrow_fixed_steps,
    check_both_directions,
    estimate_gas_cost,
    is_profitable_after_gas,
)
from core.scanner import ArbitrageScanner, HARDCODED_PAIRS

# 加载环境变量
load_dotenv(PROJECT_ROOT / ".env")


# ============================================
# 测试配置（Base Mainnet Fork）
# ============================================

# RPC 地址
RPC_URL = os.getenv("RPC_URL", "http://127.0.0.1:8545")

# 配对地址（Base Mainnet）
PAIR_BASESWAP = "0x41d160033c222e6f3722ec97379867324567d883"  # BaseSwap WETH/USDbC
PAIR_UNISWAP = "0xe902EF54E437967c8b37D30E80ff887955c90DB6"   # Uniswap V2 WETH/USDbC

# 代币地址
WETH_ADDRESS = "0x4200000000000000000000000000000000000006"
USDbC_ADDRESS = "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA"


# ============================================
# 测试结果追踪
# ============================================

class TestResults:
    """测试结果追踪器"""
    
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.tests = []
    
    def add_pass(self, name: str, message: str = ""):
        self.passed += 1
        self.tests.append((name, True, message))
        print(f"✅ {name}: {message}" if message else f"✅ {name}")
    
    def add_fail(self, name: str, message: str = ""):
        self.failed += 1
        self.tests.append((name, False, message))
        print(f"❌ {name}: {message}" if message else f"❌ {name}")
    
    def summary(self):
        print("\n" + "=" * 60)
        print("测试总结")
        print("=" * 60)
        print(f"通过: {self.passed}")
        print(f"失败: {self.failed}")
        print(f"总计: {self.passed + self.failed}")
        
        if self.failed > 0:
            print("\n失败的测试:")
            for name, passed, msg in self.tests:
                if not passed:
                    print(f"  - {name}: {msg}")
        
        return self.failed == 0


# ============================================
# 测试 1: Calculator 数学验证
# ============================================

def test_calculator_math(results: TestResults):
    """
    测试 Calculator 模块的数学逻辑
    
    验证 Uniswap V2 AMM 公式：
    amountOut = (amountIn * 997 * reserveOut) / (reserveIn * 1000 + amountIn * 997)
    """
    print("\n" + "-" * 60)
    print("测试 1: Calculator 数学验证")
    print("-" * 60)
    
    # 测试场景：
    # ReserveIn = 1000 ETH (1000 * 10^18 wei)
    # ReserveOut = 3,000,000 USDC (3,000,000 * 10^6)
    # AmountIn = 1 ETH (10^18 wei)
    
    reserve_in = 1000 * 10**18   # 1000 ETH
    reserve_out = 3_000_000 * 10**6  # 3,000,000 USDC
    amount_in = 1 * 10**18  # 1 ETH
    
    print(f"\n测试参数:")
    print(f"  ReserveIn: {reserve_in / 10**18:,.0f} ETH")
    print(f"  ReserveOut: {reserve_out / 10**6:,.0f} USDC")
    print(f"  AmountIn: {amount_in / 10**18:,.0f} ETH")
    
    # 调用我们的函数
    calculated_out = get_amount_out(amount_in, reserve_in, reserve_out)
    
    # 手动计算期望值
    # amountOut = (amountIn * 997 * reserveOut) / (reserveIn * 1000 + amountIn * 997)
    numerator = amount_in * 997 * reserve_out
    denominator = reserve_in * 1000 + amount_in * 997
    expected_out = numerator // denominator
    
    print(f"\n计算结果:")
    print(f"  期望输出: {expected_out / 10**6:,.6f} USDC")
    print(f"  计算输出: {calculated_out / 10**6:,.6f} USDC")
    
    # 验证结果
    if calculated_out == expected_out:
        results.add_pass("get_amount_out 公式", f"输出 {calculated_out / 10**6:,.6f} USDC")
    else:
        results.add_fail("get_amount_out 公式", f"期望 {expected_out}, 实际 {calculated_out}")
    
    # 测试 2: 验证 get_amount_in（反向计算）
    print("\n测试反向计算 get_amount_in...")
    
    # 使用上面的输出作为输入
    calculated_in = get_amount_in(calculated_out, reserve_in, reserve_out)
    
    # 由于精度损失，反向计算应该略大于原始输入
    print(f"  原始输入: {amount_in / 10**18:,.6f} ETH")
    print(f"  反向计算: {calculated_in / 10**18:,.6f} ETH")
    
    # 允许 0.1% 的误差
    if abs(calculated_in - amount_in) / amount_in < 0.001:
        results.add_pass("get_amount_in 反向计算", f"误差 {abs(calculated_in - amount_in) / amount_in * 100:.4f}%")
    else:
        results.add_fail("get_amount_in 反向计算", f"误差过大: {abs(calculated_in - amount_in) / amount_in * 100:.4f}%")
    
    # 测试 3: 验证闪电贷还款计算
    print("\n测试闪电贷还款计算...")
    
    borrow_amount = 10**18  # 1 ETH
    repayment = get_flash_loan_repayment(borrow_amount)
    
    # 期望还款 = borrow * 1000 / 997 + 1
    expected_repayment = (borrow_amount * 1000) // 997 + 1
    
    print(f"  借入: {borrow_amount / 10**18:,.6f} ETH")
    print(f"  还款: {repayment / 10**18:,.6f} ETH")
    print(f"  手续费: {(repayment - borrow_amount) / 10**18:,.6f} ETH ({(repayment - borrow_amount) / borrow_amount * 100:.4f}%)")
    
    if repayment == expected_repayment:
        results.add_pass("闪电贷还款计算", f"手续费率 ~0.3%")
    else:
        results.add_fail("闪电贷还款计算", f"期望 {expected_repayment}, 实际 {repayment}")
    
    # 测试 4: 边界条件
    print("\n测试边界条件...")
    
    # 零输入应返回零
    zero_out = get_amount_out(0, reserve_in, reserve_out)
    if zero_out == 0:
        results.add_pass("零输入处理", "正确返回 0")
    else:
        results.add_fail("零输入处理", f"期望 0, 实际 {zero_out}")
    
    # 零储备应返回零
    zero_reserve_out = get_amount_out(amount_in, reserve_in, 0)
    if zero_reserve_out == 0:
        results.add_pass("零储备处理", "正确返回 0")
    else:
        results.add_fail("零储备处理", f"期望 0, 实际 {zero_reserve_out}")


# ============================================
# 测试 2: Multicall 集成测试
# ============================================

def test_multicall_integration(w3: Web3, results: TestResults) -> Tuple[Optional[Tuple[int, int]], Optional[Tuple[int, int]]]:
    """
    测试 Multicall 模块的集成功能
    
    验证能够从本地 fork 批量获取真实储备数据
    """
    print("\n" + "-" * 60)
    print("测试 2: Multicall 集成测试")
    print("-" * 60)
    
    baseswap_reserves = None
    uniswap_reserves = None
    
    try:
        # 初始化 Multicall
        multicall = Multicall(w3)
        print(f"\nMulticall3 地址: {multicall.address}")
        
        # 批量获取储备
        pairs = [PAIR_BASESWAP, PAIR_UNISWAP]
        print(f"\n获取配对储备:")
        print(f"  BaseSwap: {PAIR_BASESWAP}")
        print(f"  Uniswap:  {PAIR_UNISWAP}")
        
        reserves_list = multicall.get_reserves_batch(pairs)
        
        print(f"\n获取结果:")
        
        # 检查 BaseSwap 储备
        if reserves_list[0] and reserves_list[0][0] > 0:
            r0, r1, ts = reserves_list[0]
            baseswap_reserves = (r0, r1)
            print(f"  BaseSwap:")
            print(f"    Reserve0 (WETH): {r0 / 10**18:,.6f}")
            print(f"    Reserve1 (USDbC): {r1 / 10**6:,.2f}")
            print(f"    价格: {(r1 / 10**6) / (r0 / 10**18):,.2f} USDbC/WETH")
            results.add_pass("BaseSwap 储备获取", f"{r0 / 10**18:.4f} WETH, {r1 / 10**6:.2f} USDbC")
        else:
            results.add_fail("BaseSwap 储备获取", "储备为 0 或获取失败")
        
        # 检查 Uniswap 储备
        if reserves_list[1] and reserves_list[1][0] > 0:
            r0, r1, ts = reserves_list[1]
            uniswap_reserves = (r0, r1)
            print(f"  Uniswap V2:")
            print(f"    Reserve0 (WETH): {r0 / 10**18:,.6f}")
            print(f"    Reserve1 (USDbC): {r1 / 10**6:,.2f}")
            print(f"    价格: {(r1 / 10**6) / (r0 / 10**18):,.2f} USDbC/WETH")
            results.add_pass("Uniswap V2 储备获取", f"{r0 / 10**18:.4f} WETH, {r1 / 10**6:.2f} USDbC")
        else:
            results.add_fail("Uniswap V2 储备获取", "储备为 0 或获取失败")
        
        # 验证 Multicall 效率
        import time
        
        start = time.time()
        for _ in range(10):
            multicall.get_reserves_batch(pairs)
        elapsed = (time.time() - start) / 10 * 1000  # 平均毫秒
        
        print(f"\n性能测试:")
        print(f"  平均获取耗时: {elapsed:.2f}ms")
        
        if elapsed < 100:  # 100ms 以内算合格
            results.add_pass("Multicall 性能", f"{elapsed:.2f}ms/次")
        else:
            results.add_fail("Multicall 性能", f"太慢: {elapsed:.2f}ms/次")
        
    except Exception as e:
        results.add_fail("Multicall 连接", str(e))
    
    return baseswap_reserves, uniswap_reserves


# ============================================
# 测试 3: 套利利润模拟
# ============================================

def test_profit_simulation(
    results: TestResults,
    baseswap_reserves: Optional[Tuple[int, int]],
    uniswap_reserves: Optional[Tuple[int, int]]
):
    """
    测试套利利润计算模拟
    
    使用真实储备数据模拟套利流程
    """
    print("\n" + "-" * 60)
    print("测试 3: 套利利润模拟")
    print("-" * 60)
    
    if baseswap_reserves is None or uniswap_reserves is None:
        print("\n⚠️ 跳过：缺少储备数据")
        results.add_fail("利润模拟", "缺少储备数据")
        return
    
    # 计算价格差异
    price_baseswap = (baseswap_reserves[1] / 10**6) / (baseswap_reserves[0] / 10**18)
    price_uniswap = (uniswap_reserves[1] / 10**6) / (uniswap_reserves[0] / 10**18)
    price_diff_pct = abs(price_baseswap - price_uniswap) / min(price_baseswap, price_uniswap) * 100
    
    print(f"\n价格比较:")
    print(f"  BaseSwap: {price_baseswap:,.2f} USDbC/WETH")
    print(f"  Uniswap:  {price_uniswap:,.2f} USDbC/WETH")
    print(f"  差异: {price_diff_pct:.4f}%")
    
    # 模拟借入 1 ETH 的套利
    borrow_amount = 10**18  # 1 ETH
    
    print(f"\n模拟套利（借入 {borrow_amount / 10**18} ETH）:")
    
    # 使用 calculate_arb_profit 计算
    result = calculate_arb_profit(
        borrow_amount=borrow_amount,
        pair0_reserves=baseswap_reserves,
        pair1_reserves=uniswap_reserves,
        borrow_is_token0=True
    )
    
    print(f"  正向路径 (BaseSwap -> Uniswap):")
    print(f"    Swap1 输出: {result.swap1_output / 10**6:,.4f} USDbC")
    print(f"    Swap2 输出: {result.swap2_output / 10**18:,.6f} WETH")
    print(f"    需还款: {result.repay_amount / 10**18:,.6f} WETH")
    print(f"    利润: {result.profit / 10**18:,.6f} WETH")
    print(f"    有利可图: {result.profitable}")
    
    # 计算反向路径
    result_rev = calculate_arb_profit(
        borrow_amount=borrow_amount,
        pair0_reserves=uniswap_reserves,
        pair1_reserves=baseswap_reserves,
        borrow_is_token0=True
    )
    
    print(f"\n  反向路径 (Uniswap -> BaseSwap):")
    print(f"    Swap1 输出: {result_rev.swap1_output / 10**6:,.4f} USDbC")
    print(f"    Swap2 输出: {result_rev.swap2_output / 10**18:,.6f} WETH")
    print(f"    需还款: {result_rev.repay_amount / 10**18:,.6f} WETH")
    print(f"    利润: {result_rev.profit / 10**18:,.6f} WETH")
    print(f"    有利可图: {result_rev.profitable}")
    
    # 使用 check_both_directions 找最优方向
    print(f"\n搜索最优借入金额...")
    direction, opt_amount, opt_result = check_both_directions(
        pair0_reserves=baseswap_reserves,
        pair1_reserves=uniswap_reserves,
        borrow_is_token0=True
    )
    
    print(f"  最优方向: {direction}")
    print(f"  最优借入: {opt_amount / 10**18:,.4f} ETH")
    print(f"  最大利润: {opt_result.profit / 10**18:,.6f} ETH")
    
    # 考虑 Gas 成本
    gas_cost = estimate_gas_cost(gas_price_gwei=0.01)
    is_profitable, net_profit = is_profitable_after_gas(opt_result, gas_cost)
    
    print(f"\n  Gas 成本: {gas_cost / 10**18:,.8f} ETH")
    print(f"  净利润: {net_profit / 10**18:,.6f} ETH")
    print(f"  扣除 Gas 后有利可图: {is_profitable}")
    
    # 验证代码运行成功
    results.add_pass("套利计算执行", f"最优利润 {opt_result.profit / 10**18:.6f} ETH")
    
    # 验证合理性检查
    # 在主网 fork 上，价格差异通常很小，利润可能为负
    if price_diff_pct < 0.3:  # 价格差异小于 0.3%
        if not result.profitable:
            results.add_pass("合理性检查", "价格差异小于手续费，无套利机会（预期行为）")
        else:
            results.add_pass("合理性检查", "发现潜在套利机会")
    else:
        if result.profitable:
            results.add_pass("合理性检查", "价格差异大，发现套利机会")
        else:
            results.add_fail("合理性检查", "价格差异大但未发现套利机会")


# ============================================
# 测试 4: Scanner 模块测试
# ============================================

def test_scanner_module(w3: Web3, results: TestResults):
    """
    测试 Scanner 模块功能
    """
    print("\n" + "-" * 60)
    print("测试 4: Scanner 模块测试")
    print("-" * 60)
    
    try:
        # 初始化 Scanner
        scanner = ArbitrageScanner(
            w3=w3,
            pairs=HARDCODED_PAIRS,
            gas_price_gwei=0.01
        )
        
        print(f"\nScanner 配置:")
        print(f"  监控配对数: {len(scanner.pairs)}")
        print(f"  配对组数: {len(scanner.pair_groups)}")
        
        results.add_pass("Scanner 初始化", f"{len(scanner.pairs)} 个配对")
        
        # 执行单次扫描
        import time
        start = time.time()
        opportunities = scanner.run_once()
        elapsed = (time.time() - start) * 1000
        
        print(f"\n单次扫描结果:")
        print(f"  耗时: {elapsed:.2f}ms")
        print(f"  发现机会: {len(opportunities)}")
        
        results.add_pass("Scanner 扫描执行", f"{elapsed:.2f}ms")
        
        # 获取价格
        prices = scanner.get_pair_prices()
        print(f"\n当前价格:")
        for addr, info in prices.items():
            r0 = info['reserve0'] / 10**18
            r1 = info['reserve1'] / 10**6
            price = r1 / r0 if r0 > 0 else 0
            print(f"  {info['dex']}: {price:.2f} USDbC/WETH")
        
        results.add_pass("价格获取", f"{len(prices)} 个配对")
        
        # 如果发现机会，打印详情
        if opportunities:
            print(f"\n发现的套利机会:")
            for opp in opportunities:
                print(f"  方向: {opp.direction}")
                print(f"  借入: {opp.borrow_amount / 10**18:.4f} ETH")
                print(f"  利润: {opp.profit_after_gas / 10**18:.6f} ETH")
        
    except Exception as e:
        results.add_fail("Scanner 模块", str(e))
        import traceback
        traceback.print_exc()


# ============================================
# 主函数
# ============================================

async def main():
    """异步主函数"""
    print("\n" + "=" * 60)
    print("Phase 3 测试脚本 - Brain 模块验证")
    print("=" * 60)
    
    # 初始化测试结果追踪器
    results = TestResults()
    
    # 连接到网络
    print(f"\n连接到网络: {RPC_URL}")
    w3 = Web3(Web3.HTTPProvider(RPC_URL))
    
    if not w3.is_connected():
        print("❌ 无法连接到网络")
        print("请确保 Anvil fork 正在运行:")
        print("  anvil --fork-url https://mainnet.base.org --port 8545")
        return False
    
    chain_id = w3.eth.chain_id
    block_number = w3.eth.block_number
    print(f"✅ 已连接")
    print(f"  链 ID: {chain_id}")
    print(f"  区块号: {block_number}")
    
    # 测试 1: Calculator 数学验证
    test_calculator_math(results)
    
    # 测试 2: Multicall 集成测试
    baseswap_reserves, uniswap_reserves = test_multicall_integration(w3, results)
    
    # 测试 3: 套利利润模拟
    test_profit_simulation(results, baseswap_reserves, uniswap_reserves)
    
    # 测试 4: Scanner 模块测试
    test_scanner_module(w3, results)
    
    # 输出总结
    success = results.summary()
    
    print("\n" + "=" * 60)
    if success:
        print("🎉 所有测试通过！Phase 3 模块验证成功。")
    else:
        print("⚠️ 部分测试失败，请检查上述错误。")
    print("=" * 60 + "\n")
    
    return success


def run_sync_main():
    """同步包装器"""
    return asyncio.run(main())


if __name__ == "__main__":
    try:
        success = run_sync_main()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n用户中断测试")
        sys.exit(1)
    except Exception as e:
        print(f"\n❌ 测试执行失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

