"""
FlashArb-Core 配置加载器

负责加载和验证链配置以及环境变量中的敏感信息。
将静态JSON配置与环境变量结合，实现安全的凭证管理。
"""

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv


@dataclass
class GasConfig:
    """区块链的 Gas 配置"""
    
    type: str  # "eip1559" 或 "legacy"
    priority_fee_multiplier: float = 1.1
    max_fee_multiplier: float = 1.5
    gas_price_multiplier: float = 1.1


@dataclass
class ChainConfig:
    """单个区块链的完整配置"""
    
    name: str
    chain_id: int
    rpc_urls: List[str]
    native_token: str
    wnative_address: str
    dex_routers: Dict[str, str]
    gas_config: GasConfig
    block_time: float
    
    # 敏感信息（从环境变量加载）
    private_key: Optional[str] = None
    flashbots_auth_key: Optional[str] = None
    
    # 运行时设置
    rpc_timeout: int = 10
    max_retries: int = 3


class ConfigValidationError(Exception):
    """配置验证失败时抛出的异常"""
    pass


class ConfigLoader:
    """
    FlashArb-Core 配置管理器
    
    从 JSON 文件加载链配置，并与环境变量中的敏感信息结合。
    
    使用示例:
        >>> loader = ConfigLoader()
        >>> config = loader.get_chain_config("BASE")
        >>> print(config.chain_id)  # 8453
    """
    
    def __init__(
        self,
        config_path: Optional[str] = None,
        env_path: Optional[str] = None
    ) -> None:
        """
        初始化配置加载器
        
        参数:
            config_path: chains.json 文件路径，默认为 config/chains.json
            env_path: .env 文件路径，默认为项目根目录的 .env
        """
        self._project_root = self._find_project_root()
        
        # 加载环境变量
        env_file = Path(env_path) if env_path else self._project_root / ".env"
        if env_file.exists():
            load_dotenv(env_file)
        
        # 加载链配置
        config_file = Path(config_path) if config_path else self._project_root / "config" / "chains.json"
        self._raw_config = self._load_json_config(config_file)
        
        # 已解析配置的缓存
        self._chain_cache: Dict[str, ChainConfig] = {}
        
        # 从环境变量加载全局设置
        self._private_key = os.getenv("PRIVATE_KEY")
        self._flashbots_auth_key = os.getenv("FLASHBOTS_AUTH_KEY")
        self._rpc_timeout = int(os.getenv("RPC_TIMEOUT", "10"))
        self._max_retries = int(os.getenv("MAX_RETRIES", "3"))
        self._debug_mode = os.getenv("DEBUG_MODE", "false").lower() == "true"
    
    def _find_project_root(self) -> Path:
        """
        查找项目根目录
        
        通过查找常见的项目标记（config 文件夹、.git 等）来定位
        
        返回:
            项目根目录的 Path 对象
        """
        current = Path(__file__).resolve().parent
        
        # 向上查找项目根目录（查找 config 文件夹或 .git）
        for _ in range(5):  # 最多向上查找5层
            if (current / "config").exists() or (current / ".git").exists():
                return current
            current = current.parent
        
        # 回退到 core 文件夹的父目录
        return Path(__file__).resolve().parent.parent
    
    def _load_json_config(self, path: Path) -> Dict[str, Any]:
        """
        加载并验证 JSON 配置文件
        
        参数:
            path: JSON 配置文件的路径
            
        返回:
            解析后的 JSON 字典
            
        异常:
            ConfigValidationError: 文件不存在或 JSON 格式无效
        """
        if not path.exists():
            raise ConfigValidationError(f"配置文件不存在: {path}")
        
        try:
            with open(path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except json.JSONDecodeError as e:
            raise ConfigValidationError(f"{path} 中的 JSON 格式无效: {e}")
        
        # 基本结构验证
        if not isinstance(config, dict):
            raise ConfigValidationError("配置必须是一个 JSON 对象")
        
        return config
    
    def _validate_chain_config(self, name: str, config: Dict[str, Any]) -> None:
        """
        验证单个链的配置
        
        参数:
            name: 链名称
            config: 原始链配置字典
            
        异常:
            ConfigValidationError: 缺少必需字段或字段无效
        """
        required_fields = [
            "chain_id", "rpc_urls", "native_token", 
            "wnative_address", "dex_routers", "gas_config"
        ]
        
        for field_name in required_fields:
            if field_name not in config:
                raise ConfigValidationError(
                    f"链 {name} 的配置中缺少必需字段 '{field_name}'"
                )
        
        # 验证 RPC URL 列表
        if not isinstance(config["rpc_urls"], list) or len(config["rpc_urls"]) == 0:
            raise ConfigValidationError(
                f"链 {name} 必须至少配置一个 RPC URL"
            )
        
        # 验证 gas 配置
        gas_type = config["gas_config"].get("type")
        if gas_type not in ("eip1559", "legacy"):
            raise ConfigValidationError(
                f"链 {name} 的 gas_config type 无效: 必须是 'eip1559' 或 'legacy'"
            )
        
        # 验证地址格式
        wnative = config["wnative_address"]
        if not wnative.startswith("0x") or len(wnative) != 42:
            raise ConfigValidationError(
                f"链 {name} 的 wnative_address 无效: {wnative}"
            )
    
    def _get_rpc_override(self, chain_name: str) -> Optional[List[str]]:
        """
        从环境变量获取 RPC URL 覆盖配置
        
        参数:
            chain_name: 链名称（如 "ETHEREUM"、"BASE"）
            
        返回:
            覆盖的 RPC URL 列表，如果未设置则返回 None
        """
        env_key = f"{chain_name}_RPC_OVERRIDE"
        override = os.getenv(env_key)
        
        if override:
            return [url.strip() for url in override.split(",") if url.strip()]
        return None
    
    def _parse_gas_config(self, raw_config: Dict[str, Any]) -> GasConfig:
        """
        解析 Gas 配置
        
        参数:
            raw_config: 原始 Gas 配置字典
            
        返回:
            解析后的 GasConfig 对象
        """
        return GasConfig(
            type=raw_config.get("type", "legacy"),
            priority_fee_multiplier=raw_config.get("priority_fee_multiplier", 1.1),
            max_fee_multiplier=raw_config.get("max_fee_multiplier", 1.5),
            gas_price_multiplier=raw_config.get("gas_price_multiplier", 1.1),
        )
    
    def get_chain_config(self, chain_name: str) -> ChainConfig:
        """
        获取指定链的完整配置
        
        将静态 JSON 配置与环境变量中的敏感信息合并。
        结果会被缓存以提高性能。
        
        参数:
            chain_name: 链名称（如 "BASE"、"ETHEREUM"）
            
        返回:
            包含所有配置和敏感信息的 ChainConfig 对象
            
        异常:
            ConfigValidationError: 链不存在或配置无效
        """
        chain_name = chain_name.upper()
        
        # 首先检查缓存
        if chain_name in self._chain_cache:
            return self._chain_cache[chain_name]
        
        # 获取原始配置
        if chain_name not in self._raw_config:
            available = ", ".join(self._raw_config.keys())
            raise ConfigValidationError(
                f"链 '{chain_name}' 不存在。可用的链: {available}"
            )
        
        raw = self._raw_config[chain_name]
        
        # 验证配置
        self._validate_chain_config(chain_name, raw)
        
        # 检查是否有 RPC 覆盖
        rpc_urls = self._get_rpc_override(chain_name) or raw["rpc_urls"]
        
        # 构建 ChainConfig
        config = ChainConfig(
            name=chain_name,
            chain_id=raw["chain_id"],
            rpc_urls=rpc_urls,
            native_token=raw["native_token"],
            wnative_address=raw["wnative_address"],
            dex_routers=raw["dex_routers"],
            gas_config=self._parse_gas_config(raw["gas_config"]),
            block_time=raw.get("block_time", 12),
            private_key=self._private_key,
            flashbots_auth_key=self._flashbots_auth_key,
            rpc_timeout=self._rpc_timeout,
            max_retries=self._max_retries,
        )
        
        # 缓存并返回
        self._chain_cache[chain_name] = config
        return config
    
    def get_available_chains(self) -> List[str]:
        """
        获取所有可用的链名称列表
        
        返回:
            chains.json 中配置的所有链名称
        """
        return list(self._raw_config.keys())
    
    def get_all_configs(self) -> Dict[str, ChainConfig]:
        """
        加载并返回所有链的配置
        
        返回:
            链名称到 ChainConfig 对象的字典映射
        """
        return {
            chain_name: self.get_chain_config(chain_name)
            for chain_name in self.get_available_chains()
        }
    
    @property
    def debug_mode(self) -> bool:
        """检查是否启用了调试模式"""
        return self._debug_mode
    
    @property
    def has_private_key(self) -> bool:
        """检查是否已配置私钥"""
        return self._private_key is not None and len(self._private_key) > 0


# 便捷函数用于快速访问
def load_chain_config(chain_name: str) -> ChainConfig:
    """
    快速加载单个链配置的辅助函数
    
    参数:
        chain_name: 链名称（如 "BASE"）
        
    返回:
        指定链的 ChainConfig 对象
    """
    loader = ConfigLoader()
    return loader.get_chain_config(chain_name)
