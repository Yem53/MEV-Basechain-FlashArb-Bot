// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

/**
 * @title Uniswap V3 Interface Definitions
 * @notice Minimal interfaces for V3 Flash Loan Arbitrage
 * @dev Base Mainnet specific
 */

// ============================================
// ERC20 Interface
// ============================================

interface IERC20 {
    function totalSupply() external view returns (uint256);
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
    function allowance(address owner, address spender) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function decimals() external view returns (uint8);
    function symbol() external view returns (string memory);
}

// ============================================
// Uniswap V3 Pool Interface
// ============================================

/**
 * @dev Uniswap V3 Pool - Core trading and flash loan functionality
 */
interface IUniswapV3Pool {
    /// @notice The first token of the pool (sorted by address)
    function token0() external view returns (address);
    
    /// @notice The second token of the pool
    function token1() external view returns (address);
    
    /// @notice The pool's fee in hundredths of a bip (e.g., 3000 = 0.3%)
    function fee() external view returns (uint24);
    
    /// @notice The current in-range liquidity
    function liquidity() external view returns (uint128);
    
    /**
     * @notice The pool's current price state
     * @return sqrtPriceX96 Current sqrt(price) as Q64.96
     * @return tick Current tick
     * @return observationIndex Most recent observation index
     * @return observationCardinality Current observation array size
     * @return observationCardinalityNext Next observation array size
     * @return feeProtocol Protocol fee configuration
     * @return unlocked Whether the pool is unlocked
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
     * @notice Flash loan - borrow tokens and repay in same transaction
     * @param recipient Address to receive the flash loaned tokens
     * @param amount0 Amount of token0 to borrow
     * @param amount1 Amount of token1 to borrow
     * @param data Callback data passed to uniswapV3FlashCallback
     */
    function flash(
        address recipient,
        uint256 amount0,
        uint256 amount1,
        bytes calldata data
    ) external;
}

// ============================================
// Uniswap V3 Factory Interface
// ============================================

interface IUniswapV3Factory {
    /// @notice Get pool address for token pair and fee
    function getPool(
        address tokenA,
        address tokenB,
        uint24 fee
    ) external view returns (address pool);
}

// ============================================
// Uniswap V3 Flash Callback Interface
// ============================================

/**
 * @dev Required interface to receive V3 flash loans
 */
interface IUniswapV3FlashCallback {
    /**
     * @notice Called by the pool after a flash loan
     * @param fee0 Fee owed for token0 borrow
     * @param fee1 Fee owed for token1 borrow
     * @param data Arbitrary data passed from flash() call
     */
    function uniswapV3FlashCallback(
        uint256 fee0,
        uint256 fee1,
        bytes calldata data
    ) external;
}

// ============================================
// Uniswap V3 SwapRouter Interface
// ============================================

/**
 * @dev SwapRouter02 for executing V3 swaps
 * Base Mainnet: 0x2626664c2603336E57B271c5C0b26F421741e481
 */
interface ISwapRouter {
    struct ExactInputSingleParams {
        address tokenIn;
        address tokenOut;
        uint24 fee;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
        uint160 sqrtPriceLimitX96;
    }
    
    /// @notice Swap exact input for maximum output (single hop)
    function exactInputSingle(
        ExactInputSingleParams calldata params
    ) external payable returns (uint256 amountOut);
    
    struct ExactInputParams {
        bytes path;
        address recipient;
        uint256 amountIn;
        uint256 amountOutMinimum;
    }
    
    /// @notice Swap exact input for maximum output (multi hop)
    function exactInput(
        ExactInputParams calldata params
    ) external payable returns (uint256 amountOut);
}







