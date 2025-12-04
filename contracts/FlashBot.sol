// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "./interfaces/ISwaps.sol";
import "./libraries/SafeERC20.sol";

/**
 * @title FlashBot - 闪电套利执行器
 * @author FlashArb-Core Team
 * @notice 模块化、Gas优化的闪电贷套利合约
 * @dev 设计为中央执行枢纽，支持未来扩展 V3 和 Aave
 * 
 * 设计理念：
 * - 不在状态变量中存储路由器或代币地址（节省 SLOAD gas）
 * - 所有参数通过 calldata 动态传递
 * - 使用 unchecked 优化数学运算
 * - 预授权机制避免交易中授权消耗 gas
 * 
 * 安全特性：
 * - 仅允许所有者调用关键函数
 * - 验证回调来源
 * - 利润检查确保交易有利可图
 */
contract FlashBot is IUniswapV2Callee, IPancakeCallee {
    using SafeERC20 for IERC20;

    // ============================================
    // 常量（已知 DEX Router 地址）
    // ============================================
    
    /// @notice Aerodrome Router (Base Mainnet)
    /// @dev Solidly fork，使用不同的 swap 接口
    address public constant AERODROME_ROUTER = 0xcF77a3Ba9A5CA399B7c97c74d54e5b1Beb874E43;
    
    /// @notice Aerodrome Factory (Base Mainnet)
    /// @dev 用于构建 Route 结构
    address public constant AERODROME_FACTORY = 0x420DD381b31aEf6683db6B902084cB0FFECe40Da;
    
    /// @notice Uniswap V3 SwapRouter02 (Base Mainnet)
    /// @dev Universal Router 兼容的 SwapRouter
    address public constant UNISWAP_V3_ROUTER = 0x2626664c2603336E57B271c5C0b26F421741e481;
    
    /// @notice V3 默认费率 (0.3%)
    /// @dev 常用费率: 100 (0.01%), 500 (0.05%), 3000 (0.3%), 10000 (1%)
    uint24 public constant DEFAULT_V3_FEE = 3000;

    // ============================================
    // 状态变量（最小化以节省 gas）
    // ============================================
    
    /// @notice 合约所有者
    address public owner;
    
    /// @notice 最小利润阈值（以 wei 为单位）
    /// @dev 设为 0 表示不检查利润，适用于测试
    uint256 public minProfitThreshold;
    
    /// @notice 当前正在执行闪电贷的配对/池子地址
    /// @dev 用于回调验证，执行后设为 address(1)
    ///      V2: 配对地址
    ///      V3: 池子地址（未来使用）
    ///      Aave: 不使用此变量
    address private _activePair;
    
    /// @notice 当前闪电贷发起者（用于 Aave 验证）
    /// @dev Aave 回调需要验证 initiator
    address private _activeInitiator;

    // ============================================
    // 事件
    // ============================================
    
    /// @notice 套利执行成功事件
    event ArbitrageExecuted(
        address indexed tokenBorrow,
        uint256 amountBorrowed,
        uint256 amountRepaid,
        uint256 profit
    );
    
    /// @notice 所有权转移事件
    event OwnershipTransferred(
        address indexed previousOwner,
        address indexed newOwner
    );
    
    /// @notice 路由器授权事件
    event RouterApproved(
        address indexed token,
        address indexed router
    );
    
    /// @notice 提款事件
    event Withdrawn(
        address indexed token,
        address indexed to,
        uint256 amount
    );

    // ============================================
    // 错误定义（比 require 字符串更省 gas）
    // ============================================
    
    /// @dev 非所有者调用
    error NotOwner();
    
    /// @dev 无效的回调调用者
    error InvalidCaller();
    
    /// @dev 无效的发起者
    error InvalidSender();
    
    /// @dev 无利润
    error NoProfit();
    
    /// @dev 转账失败
    error TransferFailed();
    
    /// @dev 零地址
    error ZeroAddress();

    // ============================================
    // 修饰符
    // ============================================
    
    /// @notice 仅所有者可调用
    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    // ============================================
    // 构造函数
    // ============================================
    
    /**
     * @notice 部署合约
     * @dev 将部署者设为所有者
     */
    constructor() {
        owner = msg.sender;
        emit OwnershipTransferred(address(0), msg.sender);
    }

    // ============================================
    // 接收 ETH
    // ============================================
    
    /// @notice 接收 ETH（用于 WETH 解包）
    receive() external payable {}
    
    /// @notice 回退函数
    fallback() external payable {}

    // ============================================
    // 所有者管理
    // ============================================
    
    /**
     * @notice 转移所有权
     * @param newOwner 新所有者地址
     */
    function transferOwnership(address newOwner) external onlyOwner {
        if (newOwner == address(0)) revert ZeroAddress();
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }
    
    /**
     * @notice 设置最小利润阈值
     * @param threshold 新阈值（wei）
     */
    function setMinProfitThreshold(uint256 threshold) external onlyOwner {
        minProfitThreshold = threshold;
    }

    // ============================================
    // 预授权（部署后调用，节省交易中的 gas）
    // ============================================
    
    /**
     * @notice 预授权路由器使用代币
     * @param token 代币地址
     * @param router 路由器地址
     * @dev 设置无限授权，部署后调用一次即可
     * 
     * Gas 优化：
     * - 在套利交易中不需要再次授权
     * - 使用 type(uint256).max 避免授权用尽
     */
    function approveRouter(address token, address router) external onlyOwner {
        IERC20(token).forceApprove(router, type(uint256).max);
        emit RouterApproved(token, router);
    }
    
    /**
     * @notice 批量预授权
     * @param tokens 代币地址数组
     * @param routers 路由器地址数组
     * @dev 长度必须匹配
     */
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

    // ============================================
    // 套利入口点（模块化设计）
    // ============================================
    
    /**
     * @notice 协议类型枚举
     * @dev 用于区分不同的 DEX 协议
     */
    enum Protocol {
        UNISWAP_V2,     // Uniswap V2 及分叉（SushiSwap, PancakeSwap, BaseSwap）
        SOLIDLY,        // Solidly 分叉（Aerodrome, Velodrome）
        UNISWAP_V3,     // Uniswap V3
        AAVE_V3         // Aave V3 闪电贷（未来实现）
    }
    
    /**
     * @notice V3 交换参数结构体
     * @dev 用于传递 V3 特定参数
     */
    struct V3SwapParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        uint256 amountIn;
    }
    
    /**
     * @notice Uniswap V2 闪电兑换入口
     * @param tokenBorrow 要借入的代币地址
     * @param amount 借入数量
     * @param pairAddress V2 配对合约地址（Python 端预先计算）
     * @param userData 编码的交易数据（传递给回调，不在此解码）
     * 
     * @dev userData 格式由回调函数决定，此函数只负责触发闪电贷
     *      当前 V2 格式: abi.encode(targetRouter, tradePath)
     *      未来可扩展为其他格式
     * 
     * 模块化设计：
     * - 此函数只处理 V2 特定的触发逻辑
     * - userData 原样传递给回调，不做解码
     * - 添加 V3/Aave 只需新增入口函数，无需修改此函数
     */
    function executeV2FlashSwap(
        address tokenBorrow,
        uint256 amount,
        address pairAddress,
        bytes calldata userData
    ) external onlyOwner {
        _executeV2FlashSwapInternal(tokenBorrow, amount, pairAddress, userData);
    }
    
    /**
     * @notice 向后兼容的别名函数
     * @dev 保持与旧代码的兼容性，功能与 executeV2FlashSwap 相同
     */
    function startArbitrage(
        address tokenBorrow,
        uint256 amount,
        address pairAddress,
        bytes calldata userData
    ) external onlyOwner {
        _executeV2FlashSwapInternal(tokenBorrow, amount, pairAddress, userData);
    }
    
    /**
     * @notice V2 闪电兑换内部实现
     * @dev 抽取为内部函数，避免外部调用消耗额外 gas
     */
    function _executeV2FlashSwapInternal(
        address tokenBorrow,
        uint256 amount,
        address pairAddress,
        bytes calldata userData
    ) internal {
        // 记录当前配对地址用于回调验证
        _activePair = pairAddress;
        
        // 获取配对中的代币地址
        IUniswapV2Pair pair = IUniswapV2Pair(pairAddress);
        address token0 = pair.token0();
        address token1 = pair.token1();
        
        // 确定借入哪个代币，设置输出数量
        uint256 amount0Out;
        uint256 amount1Out;
        
        if (tokenBorrow == token0) {
            amount0Out = amount;
            amount1Out = 0;
        } else {
            amount0Out = 0;
            amount1Out = amount;
        }
        
        // 调用 swap，传入非空 data 触发闪电贷回调
        // 回调将在 uniswapV2Call 或 pancakeCall 中处理
        pair.swap(amount0Out, amount1Out, address(this), userData);
        
        // 清除活跃配对（Gas 优化：设为 address(1) 比 address(0) 便宜）
        _activePair = address(1);
    }
    
    // ============================================
    // 未来扩展入口点（预留）
    // ============================================
    
    /**
     * @notice Uniswap V3 闪电贷入口（预留）
     * @param tokenBorrow 借入代币
     * @param amount 借入数量
     * @param poolAddress V3 池子地址
     * @param userData 编码的交易数据
     * 
     * @dev 未来实现时启用此函数
     *      V3 使用不同的回调: uniswapV3SwapCallback
     */
    // function executeV3FlashSwap(
    //     address tokenBorrow,
    //     uint256 amount,
    //     address poolAddress,
    //     bytes calldata userData
    // ) external onlyOwner {
    //     // TODO: 实现 V3 闪电贷触发逻辑
    //     // _activePool = poolAddress;
    //     // IUniswapV3Pool(poolAddress).swap(...);
    // }
    
    /**
     * @notice Aave V3 闪电贷入口（预留）
     * @param assets 借入资产数组
     * @param amounts 借入数量数组
     * @param userData 编码的交易数据
     * 
     * @dev 未来实现时启用此函数
     *      Aave 使用不同的回调: executeOperation
     */
    // function executeAaveFlashLoan(
    //     address[] calldata assets,
    //     uint256[] calldata amounts,
    //     bytes calldata userData
    // ) external onlyOwner {
    //     // TODO: 实现 Aave 闪电贷触发逻辑
    //     // IPool(AAVE_POOL).flashLoan(
    //     //     address(this),
    //     //     assets,
    //     //     amounts,
    //     //     modes,
    //     //     address(this),
    //     //     userData,
    //     //     0
    //     // );
    // }

    // ============================================
    // 闪电贷回调（Uniswap V2 / SushiSwap）
    // ============================================
    
    /**
     * @notice Uniswap V2 闪电贷回调
     * @param sender 发起者地址（应该是本合约）
     * @param amount0 token0 借出数量
     * @param amount1 token1 借出数量
     * @param data 编码的交易数据
     * 
     * @dev 安全检查：
     * 1. msg.sender 必须是预期的配对合约
     * 2. sender 必须是本合约
     */
    function uniswapV2Call(
        address sender,
        uint256 amount0,
        uint256 amount1,
        bytes calldata data
    ) external override {
        _executeFlashSwap(sender, amount0, amount1, data);
    }

    // ============================================
    // 闪电贷回调（PancakeSwap）
    // ============================================
    
    /**
     * @notice PancakeSwap 闪电贷回调
     * @dev 逻辑与 uniswapV2Call 相同，仅函数名不同
     */
    function pancakeCall(
        address sender,
        uint256 amount0,
        uint256 amount1,
        bytes calldata data
    ) external override {
        _executeFlashSwap(sender, amount0, amount1, data);
    }

    // ============================================
    // 核心闪电贷执行逻辑
    // ============================================
    
    /**
     * @notice 执行闪电贷套利核心逻辑（支持 V2 + Solidly）
     * @param sender 发起者地址
     * @param amount0 token0 借出数量
     * @param amount1 token1 借出数量
     * @param data 编码的交易数据
     * 
     * @dev 内部函数，由回调调用
     * 
     * 支持三种数据格式：
     * 1. 单 V2 路由器模式: abi.encode(address router, address[] path)
     * 
     * 2. 跨 V2 模式: abi.encode(address router1, address[] path1, address router2, address[] path2)
     *    - 当 data.length > 200 且 router 不是 Aerodrome
     * 
     * 3. 混合模式 (V2 + Solidly): abi.encode(address router1, address[] path1, address router2, Route[] routes)
     *    - 当 data.length > 300（包含 Route 结构）
     *    - router1: V2 Router（用于第一跳）
     *    - router2: Solidly Router（用于第二跳）
     */
    function _executeFlashSwap(
        address sender,
        uint256 amount0,
        uint256 amount1,
        bytes calldata data
    ) internal {
        // ===== 安全检查 =====
        if (msg.sender != _activePair) revert InvalidCaller();
        if (sender != address(this)) revert InvalidSender();
        
        // 获取配对信息
        IUniswapV2Pair pair = IUniswapV2Pair(msg.sender);
        address token0 = pair.token0();
        address token1 = pair.token1();
        
        // 确定借入的代币和数量
        address tokenBorrow;
        uint256 amountBorrowed;
        
        if (amount0 > 0) {
            tokenBorrow = token0;
            amountBorrowed = amount0;
        } else {
            tokenBorrow = token1;
            amountBorrowed = amount1;
        }
        
        // 记录交易前余额
        uint256 balanceBefore = IERC20(tokenBorrow).balanceOf(address(this));
        
        // ===== 解码参数并执行套利 =====
        // 根据数据长度判断模式:
        // - <= 200: 单路由器模式 (router, path)
        // - > 200, <= 320: 跨 V2/Solidly 模式 (router1, path1, router2, path2)
        // - > 320: 跨协议模式含 V3 (router1, path1, router2, path2, v3Fee1, v3Fee2)
        // 合约会自动检测 Aerodrome/V3 Router 并使用对应接口
        if (data.length > 200) {
            // 跨 DEX 模式（V2 + Solidly + V3 混合）
            _executeCrossSwap(data, amountBorrowed);
        } else {
            // 单路由器模式（向后兼容）
            _executeSingleSwap(data, amountBorrowed);
        }
        
        // ===== 计算偿还金额 =====
        uint256 amountOwed;
        unchecked {
            amountOwed = (amountBorrowed * 1000) / 997 + 1;
        }
        
        // ===== 偿还借款 =====
        IERC20(tokenBorrow).safeTransfer(msg.sender, amountOwed);
        
        // ===== 利润检查 =====
        uint256 balanceAfter = IERC20(tokenBorrow).balanceOf(address(this));
        
        if (balanceAfter < balanceBefore) {
            revert NoProfit();
        }
        
        uint256 profit;
        unchecked {
            profit = balanceAfter - balanceBefore;
        }
        
        if (profit < minProfitThreshold) {
            revert NoProfit();
        }
        
        emit ArbitrageExecuted(tokenBorrow, amountBorrowed, amountOwed, profit);
    }
    
    /**
     * @notice 执行单路由器 swap（V2、Solidly 或 V3）
     * @dev 数据格式:
     *      - V2/Solidly: abi.encode(router, path)
     *      - V3: abi.encode(router, path, v3Fee) - path 只需 [tokenIn, tokenOut]
     */
    function _executeSingleSwap(bytes calldata data, uint256 amountIn) internal {
        // 尝试解码带 V3 费率的格式
        if (data.length > 128) {
            // 可能包含 V3 费率
            (address router, address[] memory path, uint24 v3Fee) = 
                abi.decode(data, (address, address[], uint24));
            _executeSwap(router, path, amountIn, v3Fee);
        } else {
            // 标准 V2/Solidly 格式
            (address router, address[] memory path) = abi.decode(data, (address, address[]));
            _executeSwap(router, path, amountIn, 0);
        }
    }
    
    /**
     * @notice 执行跨 DEX swap（支持 V2、Solidly、V3）
     * @dev 数据格式: abi.encode(router1, path1, router2, path2, v3Fee1, v3Fee2)
     *      - v3Fee1/v3Fee2: 如果对应路由器是 V3，则使用此费率；否则传 0
     */
    function _executeCrossSwap(bytes calldata data, uint256 amountIn) internal {
        // 尝试解码带 V3 费率的格式
        if (data.length > 320) {
            // 新格式：包含 V3 费率
            (
                address router1,
                address[] memory path1,
                address router2,
                address[] memory path2,
                uint24 v3Fee1,
                uint24 v3Fee2
            ) = abi.decode(data, (address, address[], address, address[], uint24, uint24));
            
            // 第一跳
            _executeSwap(router1, path1, amountIn, v3Fee1);
            
            // 获取中间代币余额
            address intermediateToken = path1[path1.length - 1];
            uint256 intermediateAmount = IERC20(intermediateToken).balanceOf(address(this));
            
            // 第二跳
            _executeSwap(router2, path2, intermediateAmount, v3Fee2);
        } else {
            // 旧格式：不包含 V3 费率（向后兼容）
            _executeCrossV2Swap(data, amountIn);
        }
    }
    
    /**
     * @notice 执行跨 V2 DEX swap（向后兼容）
     */
    function _executeCrossV2Swap(bytes calldata data, uint256 amountIn) internal {
        (
            address router1,
            address[] memory path1,
            address router2,
            address[] memory path2
        ) = abi.decode(data, (address, address[], address, address[]));
        
        // 第一跳
        _executeV2OrSolidlySwap(router1, path1, amountIn);
        
        // 获取中间代币余额
        address intermediateToken = path1[path1.length - 1];
        uint256 intermediateAmount = IERC20(intermediateToken).balanceOf(address(this));
        
        // 第二跳
        _executeV2OrSolidlySwap(router2, path2, intermediateAmount);
    }
    
    /**
     * @notice 执行混合模式 swap (V2 + Solidly)
     * @dev 格式: (router1, path1, router2, path2)
     *      如果 router2 是 Aerodrome，会自动将 path2 转换为 Route[]
     *      避免在 userData 中传递复杂结构体
     */
    function _executeHybridSwap(bytes calldata data, uint256 amountIn) internal {
        // 使用与 _executeCrossV2Swap 相同的解码格式
        // 但如果 router2 是 Aerodrome，自动将 path2 转换为 Route[]
        (
            address router1,
            address[] memory path1,
            address router2,
            address[] memory path2
        ) = abi.decode(data, (address, address[], address, address[]));
        
        // 第一跳: V2 或 Solidly
        _executeV2OrSolidlySwap(router1, path1, amountIn);
        
        // 获取中间代币余额
        address intermediateToken = path1[path1.length - 1];
        uint256 intermediateAmount = IERC20(intermediateToken).balanceOf(address(this));
        
        // 第二跳: 根据 router2 判断
        _executeV2OrSolidlySwap(router2, path2, intermediateAmount);
    }
    
    /**
     * @notice 根据路由器类型执行 swap（支持 V2、Solidly、V3）
     * @param router 路由器地址
     * @param path 交易路径（V2/Solidly）或 [tokenIn, tokenOut]（V3）
     * @param amountIn 输入金额
     * @param v3Fee V3 费率（仅 V3 使用，其他传 0）
     */
    function _executeSwap(
        address router,
        address[] memory path,
        uint256 amountIn,
        uint24 v3Fee
    ) internal {
        if (router == UNISWAP_V3_ROUTER) {
            // Uniswap V3 swap
            _executeV3Swap(path[0], path[path.length - 1], v3Fee, amountIn);
        } else if (router == AERODROME_ROUTER) {
            // Solidly swap - 需要包含 factory 地址
            Route[] memory routes = new Route[](path.length - 1);
            for (uint256 i = 0; i < path.length - 1; i++) {
                routes[i] = Route({
                    from: path[i],
                    to: path[i + 1],
                    stable: false,
                    factory: AERODROME_FACTORY
                });
            }
            ISolidlyRouter(router).swapExactTokensForTokens(
                amountIn, 0, routes, address(this), block.timestamp + 300
            );
        } else {
            // V2 swap
            IUniswapV2Router(router).swapExactTokensForTokens(
                amountIn, 0, path, address(this), block.timestamp + 300
            );
        }
    }
    
    /**
     * @notice 向后兼容的 V2/Solidly swap 函数
     * @dev 内部调用 _executeSwap，v3Fee 传 0
     */
    function _executeV2OrSolidlySwap(
        address router,
        address[] memory path,
        uint256 amountIn
    ) internal {
        _executeSwap(router, path, amountIn, 0);
    }
    
    /**
     * @notice 执行 Uniswap V3 精确输入单跳交换
     * @param tokenIn 输入代币
     * @param tokenOut 输出代币
     * @param fee 池费率
     * @param amountIn 输入金额
     */
    function _executeV3Swap(
        address tokenIn,
        address tokenOut,
        uint24 fee,
        uint256 amountIn
    ) internal {
        // 使用默认费率如果未指定
        uint24 actualFee = fee > 0 ? fee : DEFAULT_V3_FEE;
        
        IV3SwapRouter.ExactInputSingleParams memory params = IV3SwapRouter.ExactInputSingleParams({
            tokenIn: tokenIn,
            tokenOut: tokenOut,
            fee: actualFee,
            recipient: address(this),
            deadline: block.timestamp + 300,
            amountIn: amountIn,
            amountOutMinimum: 0,  // MEV 套利不需要滑点保护
            sqrtPriceLimitX96: 0  // 不限制价格
        });
        
        IV3SwapRouter(UNISWAP_V3_ROUTER).exactInputSingle(params);
    }
    
    /**
     * @notice 执行 V3 多跳交换
     * @param path 编码的路径 (tokenIn, fee, tokenMiddle, fee, tokenOut)
     * @param amountIn 输入金额
     */
    function _executeV3MultiHop(
        bytes memory path,
        uint256 amountIn
    ) internal {
        IV3SwapRouter.ExactInputParams memory params = IV3SwapRouter.ExactInputParams({
            path: path,
            recipient: address(this),
            deadline: block.timestamp + 300,
            amountIn: amountIn,
            amountOutMinimum: 0
        });
        
        IV3SwapRouter(UNISWAP_V3_ROUTER).exactInput(params);
    }

    // ============================================
    // 资金提取（救援功能）
    // ============================================
    
    /**
     * @notice 提取 ERC20 代币
     * @param token 代币地址
     * @param to 接收地址
     * @param amount 提取数量（0 表示全部）
     */
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
    
    /**
     * @notice 提取 ETH
     * @param to 接收地址
     * @param amount 提取数量（0 表示全部）
     */
    function withdrawETH(
        address payable to,
        uint256 amount
    ) external onlyOwner {
        if (to == address(0)) revert ZeroAddress();
        
        uint256 balance = address(this).balance;
        uint256 withdrawAmount = amount == 0 ? balance : amount;
        
        (bool success,) = to.call{value: withdrawAmount}("");
        if (!success) revert TransferFailed();
        
        emit Withdrawn(address(0), to, withdrawAmount);
    }
    
    /**
     * @notice 批量提取多种代币
     * @param tokens 代币地址数组
     * @param to 接收地址
     */
    function batchWithdrawTokens(
        address[] calldata tokens,
        address to
    ) external onlyOwner {
        if (to == address(0)) revert ZeroAddress();
        
        uint256 length = tokens.length;
        for (uint256 i = 0; i < length;) {
            uint256 balance = IERC20(tokens[i]).balanceOf(address(this));
            if (balance > 0) {
                IERC20(tokens[i]).safeTransfer(to, balance);
                emit Withdrawn(tokens[i], to, balance);
            }
            unchecked { ++i; }
        }
    }

    // ============================================
    // 查询函数
    // ============================================
    
    /**
     * @notice 获取合约代币余额
     * @param token 代币地址
     * @return 余额
     */
    function getTokenBalance(address token) external view returns (uint256) {
        return IERC20(token).balanceOf(address(this));
    }
    
    /**
     * @notice 获取合约 ETH 余额
     * @return 余额
     */
    function getETHBalance() external view returns (uint256) {
        return address(this).balance;
    }
    
    /**
     * @notice 检查代币对路由器的授权额度
     * @param token 代币地址
     * @param router 路由器地址
     * @return 授权额度
     */
    function getRouterAllowance(
        address token,
        address router
    ) external view returns (uint256) {
        return IERC20(token).allowance(address(this), router);
    }

    // ============================================
    // 未来扩展预留 - Uniswap V3
    // ============================================
    
    /**
     * @notice Uniswap V3 闪电贷回调（预留）
     * @param amount0Delta token0 变化量（正数表示需要支付）
     * @param amount1Delta token1 变化量（正数表示需要支付）
     * @param data 编码的交易数据
     * 
     * @dev 实现步骤：
     *      1. 验证 msg.sender == _activePair（V3 池子）
     *      2. 解码 data 获取交易参数
     *      3. 执行套利交易
     *      4. 计算并偿还借款（V3 需要在回调中转账）
     *      5. 验证利润
     * 
     * V3 与 V2 的区别：
     * - V3 使用 int256 表示 delta（可正可负）
     * - V3 费率可变（0.05%, 0.3%, 1%）
     * - V3 需要在回调中主动转账偿还
     */
    // function uniswapV3SwapCallback(
    //     int256 amount0Delta,
    //     int256 amount1Delta,
    //     bytes calldata data
    // ) external {
    //     // 安全检查
    //     if (msg.sender != _activePair) revert InvalidCaller();
    //     
    //     // 解码参数
    //     (address targetRouter, address[] memory tradePath, uint24 fee) = 
    //         abi.decode(data, (address, address[], uint24));
    //     
    //     // 确定需要偿还的代币和数量
    //     // amount > 0 表示我们需要支付
    //     // amount < 0 表示我们收到代币
    //     
    //     // 执行套利...
    //     
    //     // 偿还借款（V3 需要主动转账）
    //     // if (amount0Delta > 0) {
    //     //     IERC20(token0).safeTransfer(msg.sender, uint256(amount0Delta));
    //     // }
    //     // if (amount1Delta > 0) {
    //     //     IERC20(token1).safeTransfer(msg.sender, uint256(amount1Delta));
    //     // }
    // }
    
    // ============================================
    // 未来扩展预留 - Aave V3
    // ============================================
    
    /**
     * @notice Aave V3 闪电贷回调（预留）
     * @param assets 借入资产地址数组
     * @param amounts 借入数量数组
     * @param premiums 费用数组（每个资产的闪电贷费用）
     * @param initiator 发起者地址
     * @param params 编码的交易数据
     * @return 成功返回 true
     * 
     * @dev 实现步骤：
     *      1. 验证 initiator == address(this)
     *      2. 解码 params 获取交易参数
     *      3. 执行套利交易
     *      4. 授权 Aave Pool 扣款（借款 + 费用）
     *      5. 验证利润
     * 
     * Aave 与 Uniswap 的区别：
     * - 可以同时借多种资产
     * - 固定费率（通常 0.05% 或 0.09%）
     * - 通过授权而非转账偿还
     * - 支持多种还款模式（modes: 0=全额还款, 1=稳定利率债务, 2=可变利率债务）
     */
    // function executeOperation(
    //     address[] calldata assets,
    //     uint256[] calldata amounts,
    //     uint256[] calldata premiums,
    //     address initiator,
    //     bytes calldata params
    // ) external returns (bool) {
    //     // 安全检查
    //     if (initiator != address(this)) revert InvalidSender();
    //     // if (msg.sender != AAVE_POOL) revert InvalidCaller();
    //     
    //     // 解码参数
    //     (address targetRouter, address[] memory tradePath) = 
    //         abi.decode(params, (address, address[]));
    //     
    //     // 执行套利...
    //     
    //     // 授权 Aave Pool 扣款偿还
    //     // for (uint256 i = 0; i < assets.length; i++) {
    //     //     uint256 amountOwed = amounts[i] + premiums[i];
    //     //     IERC20(assets[i]).safeApprove(AAVE_POOL, amountOwed);
    //     // }
    //     
    //     return true;
    // }
    
    // ============================================
    // 未来扩展预留 - Balancer
    // ============================================
    
    /**
     * @notice Balancer 闪电贷回调（预留）
     * @dev Balancer 使用 IFlashLoanRecipient 接口
     *      费率：无费用（0%），是最便宜的闪电贷来源
     */
    // function receiveFlashLoan(
    //     IERC20[] memory tokens,
    //     uint256[] memory amounts,
    //     uint256[] memory feeAmounts,
    //     bytes memory userData
    // ) external {
    //     // 实现 Balancer 闪电贷逻辑
    // }
}

