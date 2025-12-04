#!/usr/bin/env python3
"""
Uniswap V3 Flash Loan Executor

Pure V3 implementation - no V2/Solidly legacy code.

Handles:
- Building transactions for V3 flash loans
- Signing and broadcasting
- Gas estimation and Sniper Mode

Base Mainnet Constants:
- V3 Factory: 0x33128a8fC17869897dcE68Ed026d694621f6FDfD
- SwapRouter02: 0x2626664c2603336E57B271c5C0b26F421741e481
"""

import os
import time
import threading
from typing import Dict, List, Optional, Any
from dataclasses import dataclass

from web3 import Web3
from web3.contract import Contract
from eth_abi import encode
from eth_account import Account


# ============================================
# V3 Constants - Base Mainnet
# ============================================

V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
SWAP_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
WETH = "0x4200000000000000000000000000000000000006"

# Default configuration
DEFAULT_GAS_LIMIT = 500000
MAX_GAS_PRICE_GWEI = 1.0
TX_TIMEOUT = 60


@dataclass
class ExecutionResult:
    """Transaction execution result"""
    success: bool
    tx_hash: Optional[str] = None
    gas_used: int = 0
    gas_price: int = 0
    profit: int = 0
    error: Optional[str] = None
    # Timing metrics
    time_sim_ms: float = 0.0
    time_sign_ms: float = 0.0
    time_broadcast_ms: float = 0.0
    time_confirm_ms: float = 0.0
    time_total_ms: float = 0.0


class V3Executor:
    """
    Pure V3 Flash Loan Executor
    
    Executes arbitrage transactions via FlashBotV3 contract.
    """
    
    def __init__(
        self,
        w3: Web3,
        contract: Contract,
        private_key: str,
        gas_limit: int = DEFAULT_GAS_LIMIT,
        max_gas_gwei: float = MAX_GAS_PRICE_GWEI
    ):
        """
        Initialize V3 Executor.
        
        Args:
            w3: Web3 instance
            contract: FlashBotV3 contract instance
            private_key: Wallet private key
            gas_limit: Gas limit per transaction
            max_gas_gwei: Maximum gas price in gwei
        """
        self.w3 = w3
        self.contract = contract
        self.gas_limit = gas_limit
        self.max_gas_gwei = max_gas_gwei
        
        # Load account
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        self.account = Account.from_key(private_key)
        self.address = self.account.address
        
        # Nonce management
        self._nonce_lock = threading.Lock()
        self._nonce: Optional[int] = None
        self._nonce_time: float = 0
        
        # Stats
        self.tx_count = 0
        self.success_count = 0
        self.total_profit = 0
    
    def _get_nonce(self) -> int:
        """Get next available nonce."""
        with self._nonce_lock:
            now = time.time()
            
            # Refresh nonce every 5 seconds or if not set
            if self._nonce is None or now - self._nonce_time > 5:
                self._nonce = self.w3.eth.get_transaction_count(self.address, "pending")
                self._nonce_time = now
            
            nonce = self._nonce
            self._nonce += 1
            return nonce
    
    def _reset_nonce(self):
        """Reset nonce cache."""
        with self._nonce_lock:
            self._nonce = None
            self._nonce_time = 0
    
    def _get_gas_params(self, sniper_mode: bool = True) -> Dict[str, int]:
        """
        Get gas parameters (EIP-1559 or legacy).
        
        Args:
            sniper_mode: If True, boost priority fee by 20%
        
        Returns:
            Gas parameters dict
        """
        try:
            block = self.w3.eth.get_block("latest")
            base_fee = block.get("baseFeePerGas")
            
            if base_fee is not None:
                # EIP-1559
                try:
                    priority_fee = self.w3.eth.max_priority_fee
                    if sniper_mode:
                        priority_fee = int(priority_fee * 1.2)
                except Exception:
                    priority_fee = self.w3.to_wei(0.01, "gwei")
                    if sniper_mode:
                        priority_fee = int(priority_fee * 1.2)
                
                max_fee = base_fee * 2 + priority_fee
                max_allowed = self.w3.to_wei(self.max_gas_gwei, "gwei")
                if max_fee > max_allowed:
                    max_fee = max_allowed
                
                return {
                    "maxFeePerGas": max_fee,
                    "maxPriorityFeePerGas": priority_fee
                }
            else:
                # Legacy
                gas_price = self.w3.eth.gas_price
                if sniper_mode:
                    gas_price = int(gas_price * 1.2)
                
                max_allowed = self.w3.to_wei(self.max_gas_gwei, "gwei")
                if gas_price > max_allowed:
                    gas_price = max_allowed
                
                return {"gasPrice": gas_price}
                
        except Exception:
            return {"gasPrice": self.w3.to_wei(0.01, "gwei")}
    
    def _encode_swap_data(
        self,
        target_token: str,
        target_fee: int,
        min_amount_out: int = 0
    ) -> bytes:
        """
        Encode swap data for callback.
        
        Args:
            target_token: Token to swap into
            target_fee: Fee tier of target pool
            min_amount_out: Minimum output (0 for MEV)
        
        Returns:
            Encoded swap data
        """
        return encode(
            ['address', 'uint24', 'uint256'],
            [
                self.w3.to_checksum_address(target_token),
                target_fee,
                min_amount_out
            ]
        )
    
    def execute(
        self,
        pool_address: str,
        token_borrow: str,
        amount: int,
        target_token: str,
        target_fee: int,
        expected_profit: int = 0,
        dry_run: bool = False
    ) -> ExecutionResult:
        """
        Execute V3 flash loan arbitrage.
        
        Args:
            pool_address: V3 pool to borrow from
            token_borrow: Token to borrow
            amount: Amount to borrow
            target_token: Token to swap into
            target_fee: Fee tier of swap pool
            expected_profit: Expected profit for stats
            dry_run: If True, only simulate
        
        Returns:
            ExecutionResult
        """
        start_time = time.time()
        
        try:
            # Format addresses
            pool = self.w3.to_checksum_address(pool_address)
            token = self.w3.to_checksum_address(token_borrow)
            
            # Encode swap data
            swap_data = self._encode_swap_data(target_token, target_fee)
            
            # Get gas params
            gas_params = self._get_gas_params(sniper_mode=True)
            
            # Get nonce
            nonce = self._get_nonce()
            
            # Build transaction
            tx = self.contract.functions.startArbitrage(
                pool,
                token,
                amount,
                swap_data
            ).build_transaction({
                "from": self.address,
                "nonce": nonce,
                "gas": self.gas_limit,
                **gas_params
            })
            
            if dry_run:
                return ExecutionResult(
                    success=True,
                    error="Dry run - not executed",
                    time_total_ms=(time.time() - start_time) * 1000
                )
            
            # Simulate
            t_sim_start = time.time()
            try:
                self.contract.functions.startArbitrage(
                    pool, token, amount, swap_data
                ).call({"from": self.address, "gas": self.gas_limit, **gas_params})
            except Exception as e:
                self._reset_nonce()
                return ExecutionResult(
                    success=False,
                    error=f"Simulation failed: {e}",
                    time_sim_ms=(time.time() - t_sim_start) * 1000,
                    time_total_ms=(time.time() - start_time) * 1000
                )
            t_sim_ms = (time.time() - t_sim_start) * 1000
            
            # Sign
            t_sign_start = time.time()
            signed = self.account.sign_transaction(tx)
            t_sign_ms = (time.time() - t_sign_start) * 1000
            
            # Extract raw bytes (version-compatible)
            raw_tx = None
            if hasattr(signed, 'rawTransaction'):
                raw_tx = signed.rawTransaction
            elif hasattr(signed, 'raw_transaction'):
                raw_tx = signed.raw_transaction
            elif isinstance(signed, dict):
                raw_tx = signed.get('rawTransaction') or signed.get('raw_transaction')
            
            if raw_tx is None:
                self._reset_nonce()
                return ExecutionResult(
                    success=False,
                    error="Could not extract raw transaction",
                    time_sim_ms=t_sim_ms,
                    time_sign_ms=t_sign_ms,
                    time_total_ms=(time.time() - start_time) * 1000
                )
            
            # Broadcast
            t_broadcast_start = time.time()
            tx_hash = self.w3.eth.send_raw_transaction(raw_tx)
            tx_hash_hex = tx_hash.hex()
            t_broadcast_ms = (time.time() - t_broadcast_start) * 1000
            
            # Wait for confirmation
            t_confirm_start = time.time()
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=TX_TIMEOUT)
            t_confirm_ms = (time.time() - t_confirm_start) * 1000
            
            # Check result
            success = receipt["status"] == 1
            gas_used = receipt["gasUsed"]
            
            self.tx_count += 1
            if success:
                self.success_count += 1
                self.total_profit += expected_profit
            
            return ExecutionResult(
                success=success,
                tx_hash=tx_hash_hex,
                gas_used=gas_used,
                gas_price=gas_params.get("gasPrice", gas_params.get("maxFeePerGas", 0)),
                profit=expected_profit if success else 0,
                error=None if success else "Transaction reverted",
                time_sim_ms=t_sim_ms,
                time_sign_ms=t_sign_ms,
                time_broadcast_ms=t_broadcast_ms,
                time_confirm_ms=t_confirm_ms,
                time_total_ms=(time.time() - start_time) * 1000
            )
            
        except Exception as e:
            self._reset_nonce()
            self.tx_count += 1
            return ExecutionResult(
                success=False,
                error=str(e),
                time_total_ms=(time.time() - start_time) * 1000
            )
    
    def get_balance(self) -> int:
        """Get ETH balance."""
        return self.w3.eth.get_balance(self.address)
    
    def get_stats(self) -> Dict[str, Any]:
        """Get executor statistics."""
        return {
            "address": self.address,
            "tx_count": self.tx_count,
            "success_count": self.success_count,
            "success_rate": self.success_count / self.tx_count if self.tx_count > 0 else 0,
            "total_profit_wei": self.total_profit,
            "total_profit_eth": self.total_profit / 10**18,
        }

