#!/usr/bin/env python3
"""
FlashBotV3 Deployment Script

Pure V3 - no V2/Solidly legacy code.

Features:
1. Compile FlashBotV3.sol
2. Deploy to Base Mainnet
3. Approve SwapRouter for tokens
4. Save deployment to deployments.json

Usage:
    python scripts/deploy.py

Environment Variables:
    PRIVATE_KEY: Deployer private key
    RPC_URL: Network RPC endpoint
"""

import json
import os
import sys
from pathlib import Path
from datetime import datetime
from typing import Any, Dict

# Project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
from web3 import Web3

# Load environment
load_dotenv(PROJECT_ROOT / ".env")

# ============================================
# V3 Constants - Base Mainnet
# ============================================

V3_FACTORY = "0x33128a8fC17869897dcE68Ed026d694621f6FDfD"
SWAP_ROUTER = "0x2626664c2603336E57B271c5C0b26F421741e481"
WETH = "0x4200000000000000000000000000000000000006"

# Tokens to pre-approve
TOKENS_TO_APPROVE = [
    WETH,
    "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",  # USDC
    "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA",  # USDbC
    "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb",  # DAI
]

DEPLOYMENTS_FILE = PROJECT_ROOT / "deployments.json"


# ============================================
# Solidity Compiler
# ============================================

def install_solc(version: str = "0.8.19"):
    """Install Solidity compiler."""
    import solcx
    
    print(f"📦 Checking solc v{version}...")
    
    installed = solcx.get_installed_solc_versions()
    target = solcx.install.Version(version)
    
    if target not in installed:
        print(f"   Installing solc v{version}...")
        solcx.install_solc(version)
        print(f"   ✅ Installed")
    else:
        print(f"   ✅ Already installed")
    
    solcx.set_solc_version(version)


def compile_contract() -> Dict[str, Any]:
    """Compile FlashBotV3 contract."""
    import solcx
    
    print("🔨 Compiling FlashBotV3.sol...")
    
    contracts_dir = PROJECT_ROOT / "contracts"
    main_contract = contracts_dir / "FlashBotV3.sol"
    
    if not main_contract.exists():
        raise FileNotFoundError(f"Contract not found: {main_contract}")
    
    # Read all source files with correct import path mapping
    sources = {}
    
    # Read main contract
    sources["FlashBotV3.sol"] = {"content": main_contract.read_text(encoding="utf-8")}
    
    # Read interfaces - use key matching import path
    interfaces_dir = contracts_dir / "interfaces"
    if interfaces_dir.exists():
        for file in interfaces_dir.glob("*.sol"):
            # Key must match import: "./interfaces/..." -> "interfaces/..."
            key = f"interfaces/{file.name}"
            sources[key] = {"content": file.read_text(encoding="utf-8")}
    
    # Read libraries  
    libraries_dir = contracts_dir / "libraries"
    if libraries_dir.exists():
        for file in libraries_dir.glob("*.sol"):
            key = f"libraries/{file.name}"
            sources[key] = {"content": file.read_text(encoding="utf-8")}
    
    print(f"   Found {len(sources)} source files:")
    for src in sources.keys():
        print(f"     - {src}")
    
    # Fix import paths in source code for solcx compatibility
    # solcx expects imports without "./" prefix
    def fix_imports(content: str) -> str:
        content = content.replace('import "./interfaces/', 'import "interfaces/')
        content = content.replace('import "./libraries/', 'import "libraries/')
        content = content.replace('import "../interfaces/', 'import "interfaces/')
        content = content.replace('import "../libraries/', 'import "libraries/')
        return content
    
    # Apply fixes to all sources
    for key in sources:
        sources[key]["content"] = fix_imports(sources[key]["content"])
    
    # Compile
    compiled = solcx.compile_standard({
        "language": "Solidity",
        "sources": sources,
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "outputSelection": {
                "*": {"*": ["abi", "evm.bytecode.object"]}
            }
        }
    })
    
    # Extract FlashBotV3
    contract_data = compiled["contracts"]["FlashBotV3.sol"]["FlashBotV3"]
    
    abi = contract_data["abi"]
    bytecode = contract_data["evm"]["bytecode"]["object"]
    
    print(f"   ✅ Compiled successfully")
    print(f"   ABI functions: {len([x for x in abi if x.get('type') == 'function'])}")
    print(f"   Bytecode size: {len(bytecode) // 2} bytes")
    
    return {"abi": abi, "bytecode": bytecode}


# ============================================
# Deployment
# ============================================

def deploy_contract(w3: Web3, account, abi: list, bytecode: str) -> str:
    """Deploy contract to network."""
    print("\n🚀 Deploying FlashBotV3...")
    
    Contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    
    # Build transaction
    nonce = w3.eth.get_transaction_count(account.address)
    
    # Get gas params
    try:
        block = w3.eth.get_block("latest")
        base_fee = block.get("baseFeePerGas")
        
        if base_fee:
            priority_fee = w3.to_wei(0.01, "gwei")
            max_fee = base_fee * 2 + priority_fee
            gas_params = {
                "maxFeePerGas": max_fee,
                "maxPriorityFeePerGas": priority_fee
            }
        else:
            gas_params = {"gasPrice": w3.eth.gas_price}
    except Exception:
        gas_params = {"gasPrice": w3.to_wei(0.01, "gwei")}
    
    tx = Contract.constructor().build_transaction({
        "from": account.address,
        "nonce": nonce,
        "gas": 3000000,
        **gas_params
    })
    
    # Sign and send
    signed = account.sign_transaction(tx)
    
    # Extract raw bytes
    raw_tx = None
    if hasattr(signed, 'rawTransaction'):
        raw_tx = signed.rawTransaction
    elif hasattr(signed, 'raw_transaction'):
        raw_tx = signed.raw_transaction
    
    tx_hash = w3.eth.send_raw_transaction(raw_tx)
    print(f"   TX Hash: {tx_hash.hex()}")
    
    # Wait for receipt
    print("   Waiting for confirmation...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    
    if receipt["status"] != 1:
        raise Exception("Deployment failed")
    
    contract_address = receipt["contractAddress"]
    print(f"   ✅ Deployed at: {contract_address}")
    print(f"   Gas used: {receipt['gasUsed']}")
    
    return contract_address


def approve_tokens(w3: Web3, contract, account, tokens: list):
    """Approve SwapRouter for tokens."""
    print("\n🔓 Approving tokens for SwapRouter...")
    
    for token in tokens:
        try:
            print(f"   Approving {token[:10]}...")
            
            nonce = w3.eth.get_transaction_count(account.address, "pending")
            
            tx = contract.functions.approveToken(
                w3.to_checksum_address(token)
            ).build_transaction({
                "from": account.address,
                "nonce": nonce,
                "gas": 100000,
                "gasPrice": w3.eth.gas_price
            })
            
            signed = account.sign_transaction(tx)
            raw_tx = signed.rawTransaction if hasattr(signed, 'rawTransaction') else signed.raw_transaction
            tx_hash = w3.eth.send_raw_transaction(raw_tx)
            
            receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
            
            if receipt["status"] == 1:
                print(f"   ✅ Approved")
            else:
                print(f"   ❌ Failed")
                
        except Exception as e:
            print(f"   ⚠️ Error: {e}")


def save_deployment(chain_id: int, address: str, abi: list, deployer: str, tx_hash: str = ""):
    """Save deployment info to JSON."""
    print("\n💾 Saving deployment info...")
    
    deployments = {}
    if DEPLOYMENTS_FILE.exists():
        deployments = json.loads(DEPLOYMENTS_FILE.read_text())
    
    deployments[str(chain_id)] = {
        "contract_address": address,
        "contract_name": "FlashBotV3",
        "network": "Base Mainnet",
        "chain_id": chain_id,
        "deployer": deployer,
        "deployed_at": datetime.now().isoformat(),
        "tx_hash": tx_hash,
        "v3_constants": {
            "factory": V3_FACTORY,
            "swap_router": SWAP_ROUTER,
            "weth": WETH
        },
        "abi": abi
    }
    
    DEPLOYMENTS_FILE.write_text(json.dumps(deployments, indent=2))
    print(f"   ✅ Saved to {DEPLOYMENTS_FILE}")


# ============================================
# Main
# ============================================

def main():
    print("\n" + "=" * 60)
    print("     🚀 FlashBotV3 Deployment - Pure V3")
    print("=" * 60)
    
    # Get configuration
    rpc_url = os.getenv("RPC_URL")
    private_key = os.getenv("PRIVATE_KEY")
    
    if not rpc_url or not private_key:
        print("❌ Missing RPC_URL or PRIVATE_KEY in .env")
        sys.exit(1)
    
    # Connect
    print(f"\n🌐 Connecting to network...")
    w3 = Web3(Web3.HTTPProvider(rpc_url, request_kwargs={"timeout": 30}))
    
    if not w3.is_connected():
        print("❌ Failed to connect")
        sys.exit(1)
    
    chain_id = w3.eth.chain_id
    print(f"   ✅ Connected, Chain ID: {chain_id}")
    
    # Load account
    from eth_account import Account
    if not private_key.startswith("0x"):
        private_key = "0x" + private_key
    account = Account.from_key(private_key)
    
    balance = w3.eth.get_balance(account.address)
    print(f"   Deployer: {account.address}")
    print(f"   Balance: {balance / 10**18:.4f} ETH")
    
    if balance < w3.to_wei(0.01, "ether"):
        print("❌ Insufficient balance for deployment")
        sys.exit(1)
    
    # Compile
    install_solc()
    compiled = compile_contract()
    
    # Deploy
    contract_address = deploy_contract(w3, account, compiled["abi"], compiled["bytecode"])
    
    # Load deployed contract
    contract = w3.eth.contract(
        address=w3.to_checksum_address(contract_address),
        abi=compiled["abi"]
    )
    
    # Approve tokens
    approve_tokens(w3, contract, account, TOKENS_TO_APPROVE)
    
    # Save deployment
    save_deployment(chain_id, contract_address, compiled["abi"], account.address)
    
    print("\n" + "=" * 60)
    print("✅ Deployment Complete!")
    print("=" * 60)
    print(f"   Contract: {contract_address}")
    print(f"   Network:  Base Mainnet (Chain {chain_id})")
    print("\nNext steps:")
    print("   1. Update FLASHBOT_ADDRESS in .env")
    print("   2. Run: python main.py")
    print("=" * 60)


if __name__ == "__main__":
    main()
