// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * @title ISwaps - 统一交换接口
 * @notice 包含 Uniswap V2 Pair、Router 和 ERC20 的接口定义
 * @dev 为未来扩展 V3 和 Aave 预留了结构
 */

// ============================================
// ERC20 接口
// ============================================

/**
 * @dev 标准 ERC20 接口
 * 注意：某些代币（如 USDT）的 transfer 不返回布尔值
 * 必须使用 SafeERC20 库来处理这种情况
 */
interface IERC20 {
    /// @notice 返回代币总供应量
    function totalSupply() external view returns (uint256);
    
    /// @notice 返回账户余额
    function balanceOf(address account) external view returns (uint256);
    
    /// @notice 转账代币
    function transfer(address to, uint256 amount) external returns (bool);
    
    /// @notice 返回授权额度
    function allowance(address owner, address spender) external view returns (uint256);
    
    /// @notice 授权额度
    function approve(address spender, uint256 amount) external returns (bool);
    
    /// @notice 从指定地址转账
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    
    /// @notice 代币小数位
    function decimals() external view returns (uint8);
    
    /// @notice 代币符号
    function symbol() external view returns (string memory);
}

// ============================================
// Uniswap V2 接口
// ============================================

/**
 * @dev Uniswap V2 配对合约接口
 */
interface IUniswapV2Pair {
    /// @notice 返回工厂地址
    function factory() external view returns (address);
    
    /// @notice 返回 token0 地址
    function token0() external view returns (address);
    
    /// @notice 返回 token1 地址
    function token1() external view returns (address);
    
    /// @notice 返回储备量
    function getReserves() external view returns (
        uint112 reserve0,
        uint112 reserve1,
        uint32 blockTimestampLast
    );
    
    /**
     * @notice 执行兑换
     * @param amount0Out token0 输出数量
     * @param amount1Out token1 输出数量
     * @param to 接收地址
     * @param data 如果非空，将触发闪电贷回调
     */
    function swap(
        uint amount0Out,
        uint amount1Out,
        address to,
        bytes calldata data
    ) external;
    
    /// @notice 同步储备量
    function sync() external;
}

/**
 * @dev Uniswap V2 工厂接口
 */
interface IUniswapV2Factory {
    /// @notice 获取配对地址
    function getPair(address tokenA, address tokenB) external view returns (address pair);
    
    /// @notice 创建配对
    function createPair(address tokenA, address tokenB) external returns (address pair);
    
    /// @notice 获取所有配对数量
    function allPairsLength() external view returns (uint);
}

/**
 * @dev Uniswap V2 路由器接口
 */
interface IUniswapV2Router {
    /// @notice 返回工厂地址
    function factory() external pure returns (address);
    
    /// @notice 返回 WETH 地址
    function WETH() external pure returns (address);
    
    /**
     * @notice 用精确数量的代币兑换代币
     * @param amountIn 输入代币数量
     * @param amountOutMin 最小输出数量（滑点保护）
     * @param path 交易路径
     * @param to 接收地址
     * @param deadline 截止时间
     * @return amounts 每一步的数量
     */
    function swapExactTokensForTokens(
        uint amountIn,
        uint amountOutMin,
        address[] calldata path,
        address to,
        uint deadline
    ) external returns (uint[] memory amounts);
    
    /**
     * @notice 用精确数量的代币兑换代币（支持转账费用代币）
     */
    function swapExactTokensForTokensSupportingFeeOnTransferTokens(
        uint amountIn,
        uint amountOutMin,
        address[] calldata path,
        address to,
        uint deadline
    ) external;
    
    /**
     * @notice 用精确数量的 ETH 兑换代币
     */
    function swapExactETHForTokens(
        uint amountOutMin,
        address[] calldata path,
        address to,
        uint deadline
    ) external payable returns (uint[] memory amounts);
    
    /**
     * @notice 用精确数量的代币兑换 ETH
     */
    function swapExactTokensForETH(
        uint amountIn,
        uint amountOutMin,
        address[] calldata path,
        address to,
        uint deadline
    ) external returns (uint[] memory amounts);
    
    /**
     * @notice 计算输出数量
     * @param amountIn 输入数量
     * @param path 交易路径
     * @return amounts 每一步的数量
     */
    function getAmountsOut(
        uint amountIn,
        address[] calldata path
    ) external view returns (uint[] memory amounts);
    
    /**
     * @notice 计算输入数量
     * @param amountOut 输出数量
     * @param path 交易路径
     * @return amounts 每一步的数量
     */
    function getAmountsIn(
        uint amountOut,
        address[] calldata path
    ) external view returns (uint[] memory amounts);
}

// ============================================
// Uniswap V2 闪电贷回调接口
// ============================================

/**
 * @dev 闪电贷回调接口
 * Uniswap V2 和其分叉（PancakeSwap, SushiSwap 等）使用此回调
 */
interface IUniswapV2Callee {
    /**
     * @notice 闪电贷回调
     * @param sender 发起者地址
     * @param amount0 token0 借出数量
     * @param amount1 token1 借出数量
     * @param data 传递的自定义数据
     */
    function uniswapV2Call(
        address sender,
        uint amount0,
        uint amount1,
        bytes calldata data
    ) external;
}

// ============================================
// PancakeSwap 闪电贷回调接口（与 Uniswap 相同签名但不同名称）
// ============================================

/**
 * @dev PancakeSwap 闪电贷回调接口
 */
interface IPancakeCallee {
    function pancakeCall(
        address sender,
        uint amount0,
        uint amount1,
        bytes calldata data
    ) external;
}

// ============================================
// WETH 接口
// ============================================

/**
 * @dev Wrapped ETH 接口
 */
interface IWETH is IERC20 {
    /// @notice 存入 ETH 获取 WETH
    function deposit() external payable;
    
    /// @notice 取出 WETH 获取 ETH
    function withdraw(uint amount) external;
}

// ============================================
// Solidly/Aerodrome 接口
// ============================================

/**
 * @dev Solidly Router Route 结构体
 * Aerodrome 和其他 Solidly fork 使用此结构
 * 注意：Aerodrome V2 需要 factory 地址
 */
struct Route {
    address from;     // 源代币
    address to;       // 目标代币
    bool stable;      // 是否为稳定池（false = volatile）
    address factory;  // 工厂地址（Aerodrome V2 必需）
}

/**
 * @dev Solidly/Aerodrome Router 接口
 * 与 Uniswap V2 不同，使用 Route[] 而非 address[]
 */
interface ISolidlyRouter {
    /// @notice 返回工厂地址
    function factory() external pure returns (address);
    
    /// @notice 返回 WETH 地址
    function weth() external pure returns (address);
    
    /**
     * @notice 用精确数量的代币兑换代币
     * @param amountIn 输入代币数量
     * @param amountOutMin 最小输出数量（滑点保护）
     * @param routes 交易路由（Solidly 特有）
     * @param to 接收地址
     * @param deadline 截止时间
     * @return amounts 每一步的数量
     */
    function swapExactTokensForTokens(
        uint256 amountIn,
        uint256 amountOutMin,
        Route[] calldata routes,
        address to,
        uint256 deadline
    ) external returns (uint256[] memory amounts);
    
    /**
     * @notice 计算输出数量
     * @param amountIn 输入数量
     * @param routes 交易路由
     * @return amounts 每一步的数量
     */
    function getAmountsOut(
        uint256 amountIn,
        Route[] calldata routes
    ) external view returns (uint256[] memory amounts);
}

// ============================================
// 未来扩展预留：Uniswap V3
// ============================================

/**
 * @dev Uniswap V3 Swap 回调接口（预留）
 */
interface IUniswapV3SwapCallback {
    function uniswapV3SwapCallback(
        int256 amount0Delta,
        int256 amount1Delta,
        bytes calldata data
    ) external;
}

// ============================================
// 未来扩展预留：Aave Flash Loan
// ============================================

/**
 * @dev Aave 闪电贷接收者接口（预留）
 */
interface IFlashLoanReceiver {
    function executeOperation(
        address[] calldata assets,
        uint256[] calldata amounts,
        uint256[] calldata premiums,
        address initiator,
        bytes calldata params
    ) external returns (bool);
}


