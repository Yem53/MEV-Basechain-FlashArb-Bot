#!/usr/bin/env python3
"""
Uniswap V3 Flash Loan Executor - HIGH PERFORMANCE VERSION

⚡ Zero-Latency Optimizations:
1. Aggressive EIP-1559 gas strategy (2.0x priority fee)
2. Cached nonce management
3. Pre-computed gas parameters
4. Minimal RPC calls in hot path

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

# Try to import orjson for faster JSON parsing
try:
    import orjson
    HAS_ORJSON = True
except ImportError:
    HAS_ORJSON = False


# ============================================
# Configuration - Aggressive Defaults
# ============================================

V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
SWAP_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
WETH = "0x4200000000000000000000000000000000000006"

# Gas settings - Load from env
DEFAULT_GAS_LIMIT = int(os.getenv("GAS_LIMIT", "500000"))
MAX_GAS_PRICE_GWEI = float(os.getenv("MAX_GAS_GWEI", "10.0"))
TX_TIMEOUT = int(os.getenv("TX_TIMEOUT", "60"))

# Sniper Mode - Aggressive defaults for HFT
SNIPER_MODE_ENABLED = os.getenv("SNIPER_MODE_ENABLED", "true").lower() == "true"
SNIPER_MODE_MULTIPLIER = float(os.getenv("SNIPER_MODE_MULTIPLIER", "2.0"))  # 2.0x priority fee

# Nonce cache settings
NONCE_CACHE_TTL = 2  # Refresh nonce every 2 seconds (was 5)


@dataclass
class ExecutionResult:
    """Transaction execution result with timing metrics"""
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
    High-Performance V3 Flash Loan Executor
    
    ⚡ Optimizations:
    - Aggressive EIP-1559 gas strategy
    - Cached nonce with short TTL
    - Pre-formatted addresses
    - Minimal overhead in hot path
    """
    
    def __init__(
        self,
        w3: Web3,
        contract: Contract,
        private_key: str,
        gas_limit: int = DEFAULT_GAS_LIMIT,
        max_gas_gwei: float = MAX_GAS_PRICE_GWEI
    ):
        self.w3 = w3
        self.contract = contract
        self.gas_limit = gas_limit
        self.max_gas_gwei = max_gas_gwei
        self.max_gas_wei = int(max_gas_gwei * 10**9)
        
        # Load account
        if not private_key.startswith("0x"):
            private_key = "0x" + private_key
        self.account = Account.from_key(private_key)
        self.address = self.account.address
        
        # Nonce management with lock
        self._nonce_lock = threading.Lock()
        self._nonce: Optional[int] = None
        self._nonce_time: float = 0
        
        # Gas cache (refresh every scan cycle)
        self._gas_cache_lock = threading.Lock()
        self._cached_base_fee: Optional[int] = None
        self._cached_priority_fee: Optional[int] = None
        self._gas_cache_time: float = 0
        
        # Stats
        self.tx_count = 0
        self.success_count = 0
        self.total_profit = 0
    
    def _get_nonce(self) -> int:
        """Get next available nonce with caching."""
        with self._nonce_lock:
            now = time.time()
            
            # Refresh nonce if expired or not set
            if self._nonce is None or now - self._nonce_time > NONCE_CACHE_TTL:
                self._nonce = self.w3.eth.get_transaction_count(self.address, "pending")
                self._nonce_time = now
            
            nonce = self._nonce
            self._nonce += 1
            return nonce
    
    def _reset_nonce(self):
        """Reset nonce cache (call after failed tx)."""
        with self._nonce_lock:
            self._nonce = None
            self._nonce_time = 0
    
    def _refresh_gas_cache(self):
        """
        Refresh cached gas parameters.
        
        Call this once per scan cycle to minimize RPC calls.
        """
        with self._gas_cache_lock:
            now = time.time()
            
            # Only refresh if cache is stale (>1 second)
            if self._cached_base_fee is not None and now - self._gas_cache_time < 1.0:
                return
            
            try:
                block = self.w3.eth.get_block("latest")
                self._cached_base_fee = block.get("baseFeePerGas", 0)
                
                try:
                    self._cached_priority_fee = self.w3.eth.max_priority_fee
                except:
                    self._cached_priority_fee = self.w3.to_wei(0.001, "gwei")
                
                self._gas_cache_time = now
                
            except Exception:
                # Use safe defaults
                self._cached_base_fee = self.w3.to_wei(0.01, "gwei")
                self._cached_priority_fee = self.w3.to_wei(0.001, "gwei")
    
    def _get_gas_params_aggressive(self) -> Dict[str, int]:
        """
        Get AGGRESSIVE EIP-1559 gas parameters for sniping.
        
        ⚡ Strategy:
        - maxPriorityFeePerGas = network average * SNIPER_MODE_MULTIPLIER (2.0x default)
        - maxFeePerGas = baseFee * 2 + priorityFee
        
        This outbids most competitors while staying under max gas limit.
        """
        # Refresh cache if needed
        self._refresh_gas_cache()
        
        with self._gas_cache_lock:
            base_fee = self._cached_base_fee or self.w3.to_wei(0.01, "gwei")
            priority_fee = self._cached_priority_fee or self.w3.to_wei(0.001, "gwei")
        
        # Apply sniper multiplier
        if SNIPER_MODE_ENABLED:
            priority_fee = int(priority_fee * SNIPER_MODE_MULTIPLIER)
        
        # Calculate max fee: base_fee * 2 + priority_fee
        max_fee = base_fee * 2 + priority_fee
        
        # Cap at configured maximum
        if max_fee > self.max_gas_wei:
            # Scale down proportionally
            ratio = self.max_gas_wei / max_fee
            max_fee = self.max_gas_wei
            priority_fee = int(priority_fee * ratio)
        
        return {
            "maxFeePerGas": max_fee,
            "maxPriorityFeePerGas": priority_fee
        }
    
    def _get_gas_params_legacy(self) -> Dict[str, int]:
        """Get legacy gas params (fallback for non-EIP-1559 chains)."""
        try:
            gas_price = self.w3.eth.gas_price
            
            if SNIPER_MODE_ENABLED:
                gas_price = int(gas_price * SNIPER_MODE_MULTIPLIER)
            
            if gas_price > self.max_gas_wei:
                gas_price = self.max_gas_wei
            
            return {"gasPrice": gas_price}
            
        except:
            return {"gasPrice": self.w3.to_wei(0.01, "gwei")}
    
    def _get_gas_params(self) -> Dict[str, int]:
        """
        Get optimal gas parameters.
        
        Uses EIP-1559 if available, otherwise legacy.
        """
        try:
            # Check if network supports EIP-1559
            block = self.w3.eth.get_block("latest")
            if block.get("baseFeePerGas") is not None:
                return self._get_gas_params_aggressive()
            else:
                return self._get_gas_params_legacy()
        except:
            return self._get_gas_params_legacy()
    
    def _encode_swap_data(
        self,
        target_token: str,
        target_fee: int,
        min_amount_out: int = 0
    ) -> bytes:
        """Encode swap data for callback."""
        return encode(
            ['address', 'uint24', 'uint256'],
            [
                self.w3.to_checksum_address(target_token),
                target_fee,
                min_amount_out
            ]
        )
    
    def _get_raw_tx(self, signed) -> Optional[bytes]:
        """Extract raw transaction bytes (version-compatible)."""
        if hasattr(signed, 'raw_transaction'):
            return signed.raw_transaction
        elif hasattr(signed, 'rawTransaction'):
            return signed.rawTransaction
        elif isinstance(signed, dict):
            return signed.get('raw_transaction') or signed.get('rawTransaction')
        return None
    
    def refresh_gas_for_cycle(self):
        """
        Refresh gas cache for new scan cycle.
        
        Call this at the start of each scan to minimize RPC calls during execution.
        """
        self._refresh_gas_cache()
    
    def _build_access_list(
        self,
        pool_address: str,
        token0_address: str,
        token1_address: str,
        router_address: str = SWAP_ROUTER
    ) -> List[Dict[str, Any]]:
        """
        Build EIP-2930 Access List for gas optimization.
        
        ⚡ Optimization: Pre-declare storage slots we'll access.
        This reduces gas cost for "Cold SLOADs" (first access = 2100 gas -> 100 gas).
        
        We know exactly which contracts we touch:
        - V3 Pool (flash loan source)
        - Token0 (WETH or other)
        - Token1 (the paired token)
        - SwapRouter
        - Our FlashBot contract
        """
        access_list = [
            # V3 Pool - we read slot0, liquidity, and call flash()
            {
                "address": self.w3.to_checksum_address(pool_address),
                "storageKeys": []  # Empty = warm up all accessed slots
            },
            # Token0 - we read balanceOf and call transfer/approve
            {
                "address": self.w3.to_checksum_address(token0_address),
                "storageKeys": []
            },
            # Token1 - same as token0
            {
                "address": self.w3.to_checksum_address(token1_address),
                "storageKeys": []
            },
            # SwapRouter - we call exactInputSingle
            {
                "address": self.w3.to_checksum_address(router_address),
                "storageKeys": []
            },
            # Our FlashBot contract
            {
                "address": self.contract.address,
                "storageKeys": []
            }
        ]
        
        return access_list
    
    def execute(
        self,
        pool_address: str,
        token_borrow: str,
        amount: int,
        target_token: str,
        target_fee: int,
        expected_profit: int = 0,
        dry_run: bool = False,
        use_access_list: bool = True  # Enable Access List by default
    ) -> ExecutionResult:
        """
        Execute V3 flash loan arbitrage.
        
        ⚡ Optimized execution path:
        1. Pre-formatted addresses
        2. Cached gas params
        3. EIP-2930 Access Lists for gas optimization
        4. Minimal validation
        5. Fast signing
        """
        start_time = time.time()
        
        try:
            # Format addresses (checksum)
            pool = self.w3.to_checksum_address(pool_address)
            token = self.w3.to_checksum_address(token_borrow)
            target = self.w3.to_checksum_address(target_token)
            
            # Encode swap data
            swap_data = self._encode_swap_data(target_token, target_fee)
            
            # Get aggressive gas params
            gas_params = self._get_gas_params()
            
            # Get cached nonce
            nonce = self._get_nonce()
            
            # Build base transaction params
            tx_params = {
                "from": self.address,
                "nonce": nonce,
                "gas": self.gas_limit,
                **gas_params
            }
            
            # Add Access List for EIP-1559 transactions (type 0x2)
            if use_access_list and "maxFeePerGas" in gas_params:
                access_list = self._build_access_list(
                    pool_address=pool,
                    token0_address=token,  # The borrowed token
                    token1_address=target,  # The target token
                )
                tx_params["accessList"] = access_list
            
            # Build transaction
            tx = self.contract.functions.startArbitrage(
                pool,
                token,
                amount,
                swap_data
            ).build_transaction(tx_params)
            
            if dry_run:
                return ExecutionResult(
                    success=True,
                    error="Dry run - not executed",
                    gas_price=gas_params.get("maxFeePerGas", gas_params.get("gasPrice", 0)),
                    time_total_ms=(time.time() - start_time) * 1000
                )
            
            # Simulate (validation)
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
            
            # Sign transaction
            t_sign_start = time.time()
            signed = self.account.sign_transaction(tx)
            t_sign_ms = (time.time() - t_sign_start) * 1000
            
            # Extract raw bytes
            raw_tx = self._get_raw_tx(signed)
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
            
            # Update stats
            self.tx_count += 1
            if success:
                self.success_count += 1
                self.total_profit += expected_profit
            
            return ExecutionResult(
                success=success,
                tx_hash=tx_hash_hex,
                gas_used=gas_used,
                gas_price=gas_params.get("maxFeePerGas", gas_params.get("gasPrice", 0)),
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
            "gas_strategy": "EIP-1559 Aggressive" if SNIPER_MODE_ENABLED else "Standard",
            "priority_multiplier": SNIPER_MODE_MULTIPLIER,
        }
    
    def get_gas_info(self) -> Dict[str, Any]:
        """Get current gas information."""
        self._refresh_gas_cache()
        
        with self._gas_cache_lock:
            return {
                "base_fee_gwei": self._cached_base_fee / 10**9 if self._cached_base_fee else 0,
                "priority_fee_gwei": self._cached_priority_fee / 10**9 if self._cached_priority_fee else 0,
                "sniper_mode": SNIPER_MODE_ENABLED,
                "multiplier": SNIPER_MODE_MULTIPLIER,
                "max_gas_gwei": self.max_gas_gwei,
            }
