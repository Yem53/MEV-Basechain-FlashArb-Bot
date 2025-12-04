// SPDX-License-Identifier: MIT
pragma solidity ^0.8.19;

import "../interfaces/IUniswapV3.sol";

/**
 * @title SafeERC20
 * @notice 安全的 ERC20 操作封装库
 * @dev 处理不规范的 ERC20 代币（如 USDT 不返回布尔值）
 * 
 * 为什么需要这个库？
 * - USDT 等代币的 transfer/approve 函数不返回布尔值
 * - 直接调用 IERC20.transfer() 会因为返回值检查而 revert
 * - 使用低级 call 并手动检查返回值可以解决这个问题
 */
library SafeERC20 {
    
    /// @dev 转账失败错误
    error SafeTransferFailed();
    
    /// @dev 授权失败错误
    error SafeApproveFailed();
    
    /// @dev TransferFrom 失败错误
    error SafeTransferFromFailed();

    /**
     * @notice 安全转账
     * @param token ERC20 代币地址
     * @param to 接收地址
     * @param amount 转账数量
     * @dev 处理不返回值或返回 false 的情况
     */
    function safeTransfer(
        IERC20 token,
        address to,
        uint256 amount
    ) internal {
        // 使用低级 call 调用 transfer
        (bool success, bytes memory data) = address(token).call(
            abi.encodeWithSelector(IERC20.transfer.selector, to, amount)
        );
        
        // 检查调用成功且（无返回值 或 返回 true）
        if (!success || (data.length > 0 && !abi.decode(data, (bool)))) {
            revert SafeTransferFailed();
        }
    }

    /**
     * @notice 安全授权
     * @param token ERC20 代币地址
     * @param spender 被授权地址
     * @param amount 授权数量
     * @dev 处理不返回值或返回 false 的情况
     * 
     * 注意：某些代币（如 USDT）要求先将 allowance 设为 0，再设为新值
     * 这里我们假设调用者已经处理了这种情况，或者使用 forceApprove
     */
    function safeApprove(
        IERC20 token,
        address spender,
        uint256 amount
    ) internal {
        (bool success, bytes memory data) = address(token).call(
            abi.encodeWithSelector(IERC20.approve.selector, spender, amount)
        );
        
        if (!success || (data.length > 0 && !abi.decode(data, (bool)))) {
            revert SafeApproveFailed();
        }
    }

    /**
     * @notice 强制授权（先设 0 再设新值）
     * @param token ERC20 代币地址
     * @param spender 被授权地址
     * @param amount 授权数量
     * @dev 用于处理 USDT 这类需要先清零的代币
     */
    function forceApprove(
        IERC20 token,
        address spender,
        uint256 amount
    ) internal {
        // 如果当前 allowance 不为 0 且新值不为 0，先设为 0
        uint256 currentAllowance = token.allowance(address(this), spender);
        if (currentAllowance != 0 && amount != 0) {
            // 先设为 0
            (bool success, bytes memory data) = address(token).call(
                abi.encodeWithSelector(IERC20.approve.selector, spender, 0)
            );
            if (!success || (data.length > 0 && !abi.decode(data, (bool)))) {
                revert SafeApproveFailed();
            }
        }
        
        // 再设为新值
        if (amount != 0) {
            (bool success, bytes memory data) = address(token).call(
                abi.encodeWithSelector(IERC20.approve.selector, spender, amount)
            );
            if (!success || (data.length > 0 && !abi.decode(data, (bool)))) {
                revert SafeApproveFailed();
            }
        }
    }

    /**
     * @notice 安全 TransferFrom
     * @param token ERC20 代币地址
     * @param from 转出地址
     * @param to 接收地址
     * @param amount 转账数量
     */
    function safeTransferFrom(
        IERC20 token,
        address from,
        address to,
        uint256 amount
    ) internal {
        (bool success, bytes memory data) = address(token).call(
            abi.encodeWithSelector(IERC20.transferFrom.selector, from, to, amount)
        );
        
        if (!success || (data.length > 0 && !abi.decode(data, (bool)))) {
            revert SafeTransferFromFailed();
        }
    }

    /**
     * @notice 增加授权额度
     * @param token ERC20 代币地址
     * @param spender 被授权地址
     * @param addedValue 增加的数量
     */
    function safeIncreaseAllowance(
        IERC20 token,
        address spender,
        uint256 addedValue
    ) internal {
        uint256 currentAllowance = token.allowance(address(this), spender);
        safeApprove(token, spender, currentAllowance + addedValue);
    }
}


