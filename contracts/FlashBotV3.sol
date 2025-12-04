// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "./interfaces/IUniswapV3.sol";
import "./libraries/SafeERC20.sol";

/**
 * @title FlashBotV3 - Native Uniswap V3 Flash Loan Arbitrage
 * @author FlashArb Team
 * @notice Pure V3 arbitrage bot - no V2/Solidly legacy code
 * 
 * @dev Architecture:
 * 1. startArbitrage() triggers V3 pool flash()
 * 2. Pool calls uniswapV3FlashCallback()  
 * 3. Callback executes swap via SwapRouter
 * 4. Repay borrowed amount + fee
 * 5. Keep profit
 * 
 * Base Mainnet Addresses:
 * - V3 Factory: 0x33128a8fC17869897dcE68Ed026d694621f6FDfD
 * - SwapRouter02: 0x2626664c2603336E57B271c5C0b26F421741e481
 * - WETH: 0x4200000000000000000000000000000000000006
 */
contract FlashBotV3 is IUniswapV3FlashCallback {
    using SafeERC20 for IERC20;

    // ============================================
    // Constants - Base Mainnet
    // ============================================
    
    /// @notice Uniswap V3 Factory
    address public constant V3_FACTORY = 0x33128a8fC17869897dcE68Ed026d694621f6FDfD;
    
    /// @notice Uniswap V3 SwapRouter02
    address public constant SWAP_ROUTER = 0x2626664c2603336E57B271c5C0b26F421741e481;
    
    /// @notice WETH on Base
    address public constant WETH = 0x4200000000000000000000000000000000000006;
    
    /// @notice V3 Pool Init Code Hash (for address computation)
    bytes32 public constant POOL_INIT_CODE_HASH = 0xe34f199b19b2b4f47f68442619d555527d244f78a3297ea89325f843f87b8b54;

    // ============================================
    // State Variables
    // ============================================
    
    /// @notice Contract owner
    address public owner;
    
    /// @notice Minimum profit threshold (wei)
    uint256 public minProfitThreshold;
    
    /// @notice Currently active flash loan pool
    address private _activePool;

    // ============================================
    // Events
    // ============================================
    
    event ArbitrageExecuted(
        address indexed pool,
        address indexed tokenBorrow,
        uint256 amountBorrowed,
        uint256 fee,
        uint256 profit
    );
    
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event RouterApproved(address indexed token);
    event Withdrawn(address indexed token, address indexed to, uint256 amount);

    // ============================================
    // Errors
    // ============================================
    
    error NotOwner();
    error InvalidCaller();
    error NoProfit();
    error TransferFailed();
    error ZeroAddress();

    // ============================================
    // Modifiers
    // ============================================
    
    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    // ============================================
    // Constructor
    // ============================================
    
    constructor() {
        owner = msg.sender;
        emit OwnershipTransferred(address(0), msg.sender);
    }
    
    receive() external payable {}
    fallback() external payable {}

    // ============================================
    // Owner Functions
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
    // Token Approvals
    // ============================================
    
    /**
     * @notice Approve SwapRouter to spend token
     * @param token Token address to approve
     */
    function approveToken(address token) external onlyOwner {
        IERC20(token).forceApprove(SWAP_ROUTER, type(uint256).max);
        emit RouterApproved(token);
    }
    
    /**
     * @notice Batch approve multiple tokens
     * @param tokens Array of token addresses
     */
    function batchApproveTokens(address[] calldata tokens) external onlyOwner {
        uint256 len = tokens.length;
        for (uint256 i = 0; i < len;) {
            IERC20(tokens[i]).forceApprove(SWAP_ROUTER, type(uint256).max);
            emit RouterApproved(tokens[i]);
            unchecked { ++i; }
        }
    }

    // ============================================
    // V3 Flash Loan Entry Point
    // ============================================
    
    /**
     * @notice Start V3 flash loan arbitrage
     * @param pool V3 pool address (flash loan source)
     * @param tokenBorrow Token to borrow (token0 or token1 of pool)
     * @param amount Amount to borrow
     * @param swapData Encoded swap parameters for callback
     * 
     * @dev swapData format: abi.encode(targetToken, targetFee, minAmountOut)
     *      - targetToken: Token to swap borrowed funds into
     *      - targetFee: Fee tier of target pool (500, 3000, 10000)
     *      - minAmountOut: Minimum output (usually 0 for MEV)
     */
    function startArbitrage(
        address pool,
        address tokenBorrow,
        uint256 amount,
        bytes calldata swapData
    ) external onlyOwner {
        // Verify pool exists
        IUniswapV3Pool v3Pool = IUniswapV3Pool(pool);
        address token0 = v3Pool.token0();
        address token1 = v3Pool.token1();
        
        // Set active pool for callback verification
        _activePool = pool;
        
        // Determine borrow direction
        uint256 amount0 = tokenBorrow == token0 ? amount : 0;
        uint256 amount1 = tokenBorrow == token1 ? amount : 0;
        
        // Encode callback data
        bytes memory callbackData = abi.encode(tokenBorrow, amount, swapData);
        
        // Trigger flash loan
        v3Pool.flash(address(this), amount0, amount1, callbackData);
        
        // Clear active pool
        _activePool = address(1);
    }

    // ============================================
    // V3 Flash Loan Callback
    // ============================================
    
    /**
     * @notice Uniswap V3 flash loan callback
     * @param fee0 Fee for token0 borrow
     * @param fee1 Fee for token1 borrow
     * @param data Encoded callback data
     */
    function uniswapV3FlashCallback(
        uint256 fee0,
        uint256 fee1,
        bytes calldata data
    ) external override {
        // Security: verify caller is the active pool
        if (msg.sender != _activePool) revert InvalidCaller();
        
        // Decode data
        (
            address tokenBorrow,
            uint256 amountBorrow,
            bytes memory swapData
        ) = abi.decode(data, (address, uint256, bytes));
        
        // Calculate fee
        IUniswapV3Pool pool = IUniswapV3Pool(msg.sender);
        uint256 fee = tokenBorrow == pool.token0() ? fee0 : fee1;
        uint256 amountOwed = amountBorrow + fee;
        
        // Record balance before arbitrage
        uint256 balanceBefore = IERC20(tokenBorrow).balanceOf(address(this));
        
        // ===== Execute Arbitrage Swap =====
        _executeSwap(tokenBorrow, amountBorrow, swapData);
        
        // ===== Repay Flash Loan =====
        IERC20(tokenBorrow).safeTransfer(msg.sender, amountOwed);
        
        // ===== Verify Profit =====
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
        
        emit ArbitrageExecuted(msg.sender, tokenBorrow, amountBorrow, fee, profit);
    }

    // ============================================
    // Swap Execution
    // ============================================
    
    /**
     * @notice Execute V3 swap
     * @param tokenIn Input token
     * @param amountIn Input amount
     * @param swapData Encoded swap parameters
     */
    function _executeSwap(
        address tokenIn,
        uint256 amountIn,
        bytes memory swapData
    ) internal {
        (
            address tokenOut,
            uint24 fee,
            uint256 amountOutMin
        ) = abi.decode(swapData, (address, uint24, uint256));
        
        ISwapRouter.ExactInputSingleParams memory params = ISwapRouter.ExactInputSingleParams({
            tokenIn: tokenIn,
            tokenOut: tokenOut,
            fee: fee,
            recipient: address(this),
            amountIn: amountIn,
            amountOutMinimum: amountOutMin,
            sqrtPriceLimitX96: 0
        });
        
        ISwapRouter(SWAP_ROUTER).exactInputSingle(params);
    }
    
    /**
     * @notice Execute multi-hop V3 swap
     * @param amountIn Input amount
     * @param path Encoded path (token0, fee0, token1, fee1, token2, ...)
     * @param amountOutMin Minimum output
     */
    function _executeMultiHopSwap(
        uint256 amountIn,
        bytes memory path,
        uint256 amountOutMin
    ) internal {
        ISwapRouter.ExactInputParams memory params = ISwapRouter.ExactInputParams({
            path: path,
            recipient: address(this),
            amountIn: amountIn,
            amountOutMinimum: amountOutMin
        });
        
        ISwapRouter(SWAP_ROUTER).exactInput(params);
    }

    // ============================================
    // Pool Address Computation
    // ============================================
    
    /**
     * @notice Compute V3 pool address deterministically
     * @param tokenA First token
     * @param tokenB Second token
     * @param fee Pool fee tier
     * @return pool Computed pool address
     */
    function computePoolAddress(
        address tokenA,
        address tokenB,
        uint24 fee
    ) public pure returns (address pool) {
        // Sort tokens
        (address token0, address token1) = tokenA < tokenB 
            ? (tokenA, tokenB) 
            : (tokenB, tokenA);
        
        // Compute CREATE2 address
        pool = address(uint160(uint256(keccak256(abi.encodePacked(
            hex'ff',
            V3_FACTORY,
            keccak256(abi.encode(token0, token1, fee)),
            POOL_INIT_CODE_HASH
        )))));
    }

    // ============================================
    // Withdrawal Functions
    // ============================================
    
    function withdrawToken(address token, address to, uint256 amount) external onlyOwner {
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
    // View Functions
    // ============================================
    
    function getTokenBalance(address token) external view returns (uint256) {
        return IERC20(token).balanceOf(address(this));
    }
    
    function getETHBalance() external view returns (uint256) {
        return address(this).balance;
    }
    
    function getRouterAllowance(address token) external view returns (uint256) {
        return IERC20(token).allowance(address(this), SWAP_ROUTER);
    }
}

