// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * @title Uniswap V3 接口定义
 * @notice 用于 V3 闪电贷套利的完整接口
 */

// ============================================
// V3 Pool 接口
// ============================================

/**
 * @dev Uniswap V3 Pool 接口
 * @notice 核心 V3 池合约，提供闪电贷功能
 */
interface IUniswapV3Pool {
    /// @notice 返回 token0 地址
    function token0() external view returns (address);
    
    /// @notice 返回 token1 地址
    function token1() external view returns (address);
    
    /// @notice 返回池费率
    function fee() external view returns (uint24);
    
    /// @notice 返回当前流动性
    function liquidity() external view returns (uint128);
    
    /**
     * @notice 返回池的当前状态
     * @return sqrtPriceX96 当前价格的平方根（Q64.96 格式）
     * @return tick 当前 tick
     * @return observationIndex 最近的观察索引
     * @return observationCardinality 观察数组的大小
     * @return observationCardinalityNext 下一个观察数组的大小
     * @return feeProtocol 协议费率
     * @return unlocked 池是否已解锁
     */
    function slot0() external view returns (
        uint160 sqrtPriceX96,
        int24 tick,
        uint16 observationIndex,
        uint16 observationCardinality,
        uint16 observationCardinalityNext,
        uint8 feeProtocol,
        bool unlocked
    );
    
    /**
     * @notice 执行闪电贷
     * @param recipient 接收借出代币的地址
     * @param amount0 借出的 token0 数量
     * @param amount1 借出的 token1 数量
     * @param data 传递给回调的数据
     * @dev 借出后必须在回调中偿还 amount + fee
     */
    function flash(
        address recipient,
        uint256 amount0,
        uint256 amount1,
        bytes calldata data
    ) external;
    
    /**
     * @notice 执行 swap
     * @param recipient 接收代币的地址
     * @param zeroForOne 交易方向（true: token0 -> token1）
     * @param amountSpecified 交易数量（正数: exactInput, 负数: exactOutput）
     * @param sqrtPriceLimitX96 价格限制
     * @param data 传递给回调的数据
     */
    function swap(
        address recipient,
        bool zeroForOne,
        int256 amountSpecified,
        uint160 sqrtPriceLimitX96,
        bytes calldata data
    ) external returns (int256 amount0, int256 amount1);
}

// ============================================
// V3 Factory 接口
// ============================================

/**
 * @dev Uniswap V3 Factory 接口
 */
interface IUniswapV3Factory {
    /// @notice 获取池地址
    /// @param tokenA 代币 A
    /// @param tokenB 代币 B
    /// @param fee 费率
    /// @return pool 池地址
    function getPool(
        address tokenA,
        address tokenB,
        uint24 fee
    ) external view returns (address pool);
    
    /// @notice 创建池
    function createPool(
        address tokenA,
        address tokenB,
        uint24 fee
    ) external returns (address pool);
}

// ============================================
// V3 Flash Callback 接口
// ============================================

/**
 * @dev V3 闪电贷回调接口
 * @notice 实现此接口以接收 V3 闪电贷回调
 */
interface IUniswapV3FlashCallback {
    /**
     * @notice 闪电贷回调
     * @param fee0 需要支付的 token0 费用
     * @param fee1 需要支付的 token1 费用
     * @param data 传递的自定义数据
     * @dev 必须在此函数中偿还借款 + 费用
     */
    function uniswapV3FlashCallback(
        uint256 fee0,
        uint256 fee1,
        bytes calldata data
    ) external;
}

// ============================================
// V3 Swap Callback 接口
// ============================================

/**
 * @dev V3 Swap 回调接口
 */
interface IUniswapV3SwapCallback {
    /**
     * @notice Swap 回调
     * @param amount0Delta token0 变化量（正数需支付）
     * @param amount1Delta token1 变化量（正数需支付）
     * @param data 传递的自定义数据
     */
    function uniswapV3SwapCallback(
        int256 amount0Delta,
        int256 amount1Delta,
        bytes calldata data
    ) external;
}

// ============================================
// V3 SwapRouter 接口
// ============================================

/**
 * @dev Uniswap V3 SwapRouter02 接口
 * @notice Base Mainnet: 0x2626664c2603336E57B271c5C0b26F421741e481
 */
interface ISwapRouter {
    /**
     * @notice 精确输入单跳交换参数
     */
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    
    /**
     * @notice 执行精确输入单跳交换
     */
    function exactInputSingle(
        ExactInputSingleParams calldata params
    ) external payable returns (uint256 amountOut);
    
    /**
     * @notice 精确输入多跳交换参数
     */
    struct ExactInputParams {
        bytes path;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
    }
    
    /**
     * @notice 执行精确输入多跳交换
     */
    function exactInput(
        ExactInputParams calldata params
    ) external payable returns (uint256 amountOut);
    
    /**
     * @notice 精确输出单跳交换参数
     */
    struct ExactOutputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountOut;
        uint256 amountInMaximum;
        uint160 sqrtPriceLimitX96;
    }
    
    /**
     * @notice 执行精确输出单跳交换
     */
    function exactOutputSingle(
        ExactOutputSingleParams calldata params
    ) external payable returns (uint256 amountIn);
}

// ============================================
// 辅助结构体
// ============================================

/**
 * @dev V3 池信息结构体
 */
struct V3PoolInfo {
    address pool;
    address token0;
    address token1;
    uint24 fee;
    uint160 sqrtPriceX96;
    uint128 liquidity;
}

/**
 * @dev 闪电贷参数结构体
 */
struct FlashParams {
    address pool;           // V3 池地址
    address tokenBorrow;    // 借入的代币
    uint256 amountBorrow;   // 借入数量
    address swapRouter;     // 用于套利的路由器
    bytes swapData;         // 套利交易数据
}

