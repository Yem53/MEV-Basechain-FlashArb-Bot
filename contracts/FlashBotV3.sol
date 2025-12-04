// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "./interfaces/IUniswapV3.sol";
import "./interfaces/ISwaps.sol";
import "./libraries/SafeERC20.sol";

/**
 * @title FlashBotV3 - 原生 Uniswap V3 闪电贷套利执行器
 * @author FlashArb-Core Team
 * @notice 使用 Uniswap V3 原生闪电贷（0.05% 费率）进行套利
 * 
 * @dev 核心优势：
 * - V3 闪电贷费率更低（0.05% vs V2 的 0.3%）
 * - 支持精确的价格限制
 * - 更高的资本效率
 * 
 * 套利流程：
 * 1. 调用 startArbitrage() 触发 V3 池的 flash()
 * 2. 池调用 uniswapV3FlashCallback()
 * 3. 在回调中执行套利交易
 * 4. 偿还借款 + 费用
 * 5. 保留利润
 */
contract FlashBotV3 is IUniswapV3FlashCallback {
    using SafeERC20 for IERC20;

    // ============================================
    // 常量 - Base Mainnet
    // ============================================
    
    /// @notice Uniswap V3 Factory
    address public constant V3_FACTORY = 0x33128a8fC17869897dcE68Ed026d694621f6FDfD;
    
    /// @notice Uniswap V3 SwapRouter02
    address public constant V3_ROUTER = 0x2626664c2603336E57B271c5C0b26F421741e481;
    
    /// @notice WETH 地址
    address public constant WETH = 0x4200000000000000000000000000000000000006;
    
    /// @notice Aerodrome Router (备用 V2 交换)
    address public constant AERODROME_ROUTER = 0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43;
    
    /// @notice Aerodrome Factory
    address public constant AERODROME_FACTORY = 0x420DD381b31aEf6683db6B902084cB0FFECe40Da;
    
    /// @notice V3 常用费率
    uint24 public constant FEE_LOWEST = 100;    // 0.01%
    uint24 public constant FEE_LOW = 500;       // 0.05%
    uint24 public constant FEE_MEDIUM = 3000;   // 0.3%
    uint24 public constant FEE_HIGH = 10000;    // 1%
    
    /// @notice V3 池 INIT_CODE_HASH (Base Mainnet)
    bytes32 public constant POOL_INIT_CODE_HASH = 0xe34f199b19b2b4f47f68442619d555527d244f78a3297ea89325f843f87b8b54;

    // ============================================
    // 状态变量
    // ============================================
    
    /// @notice 合约所有者
    address public owner;
    
    /// @notice 最小利润阈值 (wei)
    uint256 public minProfitThreshold;
    
    /// @notice 当前活跃的闪电贷池
    address private _activePool;

    // ============================================
    // 事件
    // ============================================
    
    event ArbitrageExecuted(
        address indexed pool,
        address indexed tokenBorrow,
        uint256 amountBorrowed,
        uint256 fee,
        uint256 profit
    );
    
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event RouterApproved(address indexed token, address indexed router);
    event Withdrawn(address indexed token, address indexed to, uint256 amount);

    // ============================================
    // 错误
    // ============================================
    
    error NotOwner();
    error InvalidCaller();
    error InvalidPool();
    error NoProfit();
    error TransferFailed();
    error ZeroAddress();
    error InsufficientRepayment();

    // ============================================
    // 修饰符
    // ============================================
    
    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    // ============================================
    // 构造函数
    // ============================================
    
    constructor() {
        owner = msg.sender;
        emit OwnershipTransferred(address(0), msg.sender);
    }
    
    receive() external payable {}
    fallback() external payable {}

    // ============================================
    // 所有者管理
    // ============================================
    
    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert ZeroAddress();
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }
    
    function setMinProfitThreshold(uint256 threshold) external onlyOwner {
        minProfitThreshold = threshold;
    }

    // ============================================
    // 预授权
    // ============================================
    
    function approveRouter(address token, address router) external onlyOwner {
        IERC20(token).forceApprove(router, type(uint256).max);
        emit RouterApproved(token, router);
    }
    
    function batchApproveRouters(
        address[] calldata tokens,
        address[] calldata routers
    ) external onlyOwner {
        uint256 length = tokens.length;
        for (uint256 i = 0; i < length;) {
            IERC20(tokens[i]).forceApprove(routers[i], type(uint256).max);
            emit RouterApproved(tokens[i], routers[i]);
            unchecked { ++i; }
        }
    }
    
    /**
     * @notice 批量授权常用路由器
     * @param token 代币地址
     */
    function approveAllRouters(address token) external onlyOwner {
        IERC20(token).forceApprove(V3_ROUTER, type(uint256).max);
        IERC20(token).forceApprove(AERODROME_ROUTER, type(uint256).max);
        emit RouterApproved(token, V3_ROUTER);
        emit RouterApproved(token, AERODROME_ROUTER);
    }

    // ============================================
    // V3 闪电贷入口
    // ============================================
    
    /**
     * @notice 启动 V3 闪电贷套利
     * @param poolAddress V3 池地址（借贷源）
     * @param tokenBorrow 借入的代币地址
     * @param amountBorrow 借入数量
     * @param userData 套利交易数据
     * 
     * @dev userData 格式:
     *      abi.encode(swapType, router, swapData)
     *      - swapType: 0 = V3, 1 = V2, 2 = Solidly
     *      - router: 交换路由器地址
     *      - swapData: 具体交换参数
     */
    function startArbitrage(
        address poolAddress,
        address tokenBorrow,
        uint256 amountBorrow,
        bytes calldata userData
    ) external onlyOwner {
        // 验证池地址
        IUniswapV3Pool pool = IUniswapV3Pool(poolAddress);
        address token0 = pool.token0();
        address token1 = pool.token1();
        
        // 设置活跃池（用于回调验证）
        _activePool = poolAddress;
        
        // 确定借入方向
        uint256 amount0 = tokenBorrow == token0 ? amountBorrow : 0;
        uint256 amount1 = tokenBorrow == token1 ? amountBorrow : 0;
        
        // 编码回调数据
        bytes memory callbackData = abi.encode(
            tokenBorrow,
            amountBorrow,
            userData
        );
        
        // 触发闪电贷
        pool.flash(address(this), amount0, amount1, callbackData);
        
        // 清除活跃池
        _activePool = address(1);
    }
    
    /**
     * @notice 向后兼容的别名
     */
    function executeV3FlashLoan(
        address poolAddress,
        address tokenBorrow,
        uint256 amountBorrow,
        bytes calldata userData
    ) external onlyOwner {
        this.startArbitrage(poolAddress, tokenBorrow, amountBorrow, userData);
    }

    // ============================================
    // V3 闪电贷回调
    // ============================================
    
    /**
     * @notice Uniswap V3 闪电贷回调
     * @param fee0 token0 的费用
     * @param fee1 token1 的费用
     * @param data 编码的回调数据
     */
    function uniswapV3FlashCallback(
        uint256 fee0,
        uint256 fee1,
        bytes calldata data
    ) external override {
        // 安全检查：验证调用者是活跃的 V3 池
        if (msg.sender != _activePool) revert InvalidCaller();
        
        // 解码数据
        (
            address tokenBorrow,
            uint256 amountBorrow,
            bytes memory userData
        ) = abi.decode(data, (address, uint256, bytes));
        
        // 计算费用
        uint256 fee = tokenBorrow == IUniswapV3Pool(msg.sender).token0() ? fee0 : fee1;
        uint256 amountOwed = amountBorrow + fee;
        
        // 记录交易前余额
        uint256 balanceBefore = IERC20(tokenBorrow).balanceOf(address(this));
        
        // ===== 执行套利交易 =====
        _executeArbitrage(tokenBorrow, amountBorrow, userData);
        
        // ===== 偿还借款 + 费用 =====
        uint256 balanceAfter = IERC20(tokenBorrow).balanceOf(address(this));
        
        // 确保有足够的余额偿还
        if (balanceAfter < amountOwed) {
            revert InsufficientRepayment();
        }
        
        // 偿还给池
        IERC20(tokenBorrow).safeTransfer(msg.sender, amountOwed);
        
        // ===== 利润检查 =====
        uint256 finalBalance = IERC20(tokenBorrow).balanceOf(address(this));
        
        if (finalBalance < balanceBefore) {
            revert NoProfit();
        }
        
        uint256 profit;
        unchecked {
            profit = finalBalance - balanceBefore;
        }
        
        if (profit < minProfitThreshold) {
            revert NoProfit();
        }
        
        emit ArbitrageExecuted(msg.sender, tokenBorrow, amountBorrow, fee, profit);
    }

    // ============================================
    // 套利执行逻辑
    // ============================================
    
    /**
     * @notice 执行套利交易
     * @param tokenBorrow 借入的代币
     * @param amountBorrow 借入数量
     * @param userData 交易数据
     */
    function _executeArbitrage(
        address tokenBorrow,
        uint256 amountBorrow,
        bytes memory userData
    ) internal {
        // 解码交换类型
        (uint8 swapType, bytes memory swapParams) = abi.decode(userData, (uint8, bytes));
        
        if (swapType == 0) {
            // V3 单跳或多跳交换
            _executeV3Swap(swapParams, amountBorrow);
        } else if (swapType == 1) {
            // V2 交换（通过 Uniswap V2 Router）
            _executeV2Swap(swapParams, amountBorrow);
        } else if (swapType == 2) {
            // Solidly 交换（Aerodrome）
            _executeSolidlySwap(swapParams, amountBorrow);
        } else if (swapType == 3) {
            // 跨协议套利（两跳）
            _executeCrossProtocolArbitrage(swapParams, amountBorrow);
        }
    }
    
    /**
     * @notice 执行 V3 交换
     */
    function _executeV3Swap(bytes memory params, uint256 amountIn) internal {
        (
            address tokenIn,
            address tokenOut,
            uint24 fee
        ) = abi.decode(params, (address, address, uint24));
        
        ISwapRouter.ExactInputSingleParams memory swapParams = ISwapRouter.ExactInputSingleParams({
            tokenIn: tokenIn,
            tokenOut: tokenOut,
            fee: fee,
            recipient: address(this),
            amountIn: amountIn,
            amountOutMinimum: 0,
            sqrtPriceLimitX96: 0
        });
        
        ISwapRouter(V3_ROUTER).exactInputSingle(swapParams);
    }
    
    /**
     * @notice 执行 V3 多跳交换
     */
    function _executeV3MultiHop(bytes memory path, uint256 amountIn) internal {
        ISwapRouter.ExactInputParams memory params = ISwapRouter.ExactInputParams({
            path: path,
            recipient: address(this),
            amountIn: amountIn,
            amountOutMinimum: 0
        });
        
        ISwapRouter(V3_ROUTER).exactInput(params);
    }
    
    /**
     * @notice 执行 V2 交换
     */
    function _executeV2Swap(bytes memory params, uint256 amountIn) internal {
        (
            address router,
            address[] memory path
        ) = abi.decode(params, (address, address[]));
        
        IUniswapV2Router(router).swapExactTokensForTokens(
            amountIn,
            0,
            path,
            address(this),
            block.timestamp + 300
        );
    }
    
    /**
     * @notice 执行 Solidly 交换
     */
    function _executeSolidlySwap(bytes memory params, uint256 amountIn) internal {
        (address[] memory path) = abi.decode(params, (address[]));
        
        Route[] memory routes = new Route[](path.length - 1);
        for (uint256 i = 0; i < path.length - 1; i++) {
            routes[i] = Route({
                from: path[i],
                to: path[i + 1],
                stable: false,
                factory: AERODROME_FACTORY
            });
        }
        
        ISolidlyRouter(AERODROME_ROUTER).swapExactTokensForTokens(
            amountIn,
            0,
            routes,
            address(this),
            block.timestamp + 300
        );
    }
    
    /**
     * @notice 跨协议套利（两跳）
     * @dev 格式: (swapType1, params1, swapType2, params2, intermediateToken)
     */
    function _executeCrossProtocolArbitrage(bytes memory params, uint256 amountIn) internal {
        (
            uint8 swapType1,
            bytes memory params1,
            uint8 swapType2,
            bytes memory params2,
            address intermediateToken
        ) = abi.decode(params, (uint8, bytes, uint8, bytes, address));
        
        // 第一跳
        if (swapType1 == 0) {
            _executeV3Swap(params1, amountIn);
        } else if (swapType1 == 1) {
            _executeV2Swap(params1, amountIn);
        } else {
            _executeSolidlySwap(params1, amountIn);
        }
        
        // 获取中间代币余额
        uint256 intermediateAmount = IERC20(intermediateToken).balanceOf(address(this));
        
        // 第二跳
        if (swapType2 == 0) {
            _executeV3Swap(params2, intermediateAmount);
        } else if (swapType2 == 1) {
            _executeV2Swap(params2, intermediateAmount);
        } else {
            _executeSolidlySwap(params2, intermediateAmount);
        }
    }

    // ============================================
    // V3 池地址计算
    // ============================================
    
    /**
     * @notice 计算 V3 池地址
     * @param tokenA 代币 A
     * @param tokenB 代币 B
     * @param fee 费率
     * @return pool 池地址
     */
    function computePoolAddress(
        address tokenA,
        address tokenB,
        uint24 fee
    ) public pure returns (address pool) {
        // 排序代币地址
        (address token0, address token1) = tokenA < tokenB 
            ? (tokenA, tokenB) 
            : (tokenB, tokenA);
        
        // 计算 CREATE2 地址
        pool = address(uint160(uint256(keccak256(abi.encodePacked(
            hex'ff',
            V3_FACTORY,
            keccak256(abi.encode(token0, token1, fee)),
            POOL_INIT_CODE_HASH
        )))));
    }
    
    /**
     * @notice 从工厂获取池地址
     */
    function getPoolFromFactory(
        address tokenA,
        address tokenB,
        uint24 fee
    ) external view returns (address) {
        return IUniswapV3Factory(V3_FACTORY).getPool(tokenA, tokenB, fee);
    }

    // ============================================
    // 资金管理
    // ============================================
    
    function withdrawToken(
        address token,
        address to,
        uint256 amount
    ) external onlyOwner {
        if (to == address(0)) revert ZeroAddress();
        
        uint256 balance = IERC20(token).balanceOf(address(this));
        uint256 withdrawAmount = amount == 0 ? balance : amount;
        
        IERC20(token).safeTransfer(to, withdrawAmount);
        emit Withdrawn(token, to, withdrawAmount);
    }
    
    function withdrawETH(address payable to, uint256 amount) external onlyOwner {
        if (to == address(0)) revert ZeroAddress();
        
        uint256 balance = address(this).balance;
        uint256 withdrawAmount = amount == 0 ? balance : amount;
        
        (bool success,) = to.call{value: withdrawAmount}("");
        if (!success) revert TransferFailed();
        
        emit Withdrawn(address(0), to, withdrawAmount);
    }

    // ============================================
    // 查询函数
    // ============================================
    
    function getTokenBalance(address token) external view returns (uint256) {
        return IERC20(token).balanceOf(address(this));
    }
    
    function getETHBalance() external view returns (uint256) {
        return address(this).balance;
    }
    
    function getRouterAllowance(address token, address router) external view returns (uint256) {
        return IERC20(token).allowance(address(this), router);
    }
}

