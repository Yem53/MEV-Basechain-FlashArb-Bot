"""
FlashArb-Core ABI 加载器

用于从本地文件加载和缓存合约 ABI 的工具模块。
支持单个 ABI 加载和带缓存的批量加载。

⚡ 高性能优化:
- 使用 orjson 进行快速 JSON 解析 (10x faster)
"""

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

# Try to import orjson for faster JSON parsing
try:
    import orjson
    HAS_ORJSON = True
except ImportError:
    HAS_ORJSON = False


def _json_loads(data: bytes) -> Any:
    """Fast JSON loading with orjson fallback to stdlib json."""
    if HAS_ORJSON:
        return orjson.loads(data)
    return json.loads(data)


def _json_load_file(file_path: Path) -> Any:
    """Load JSON from file using fastest available method."""
    if HAS_ORJSON:
        with open(file_path, "rb") as f:  # orjson works with bytes
            return orjson.loads(f.read())
    else:
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)


class ABILoadError(Exception):
    """ABI 加载失败时抛出的异常"""
    pass


# 已加载 ABI 的缓存（模块级别）
_abi_cache: Dict[str, List[Dict[str, Any]]] = {}


def _find_abis_directory() -> Path:
    """
    定位 ABIs 目录
    
    在项目结构的常见位置中搜索。
    
    返回:
        abis 目录的 Path 对象
        
    异常:
        ABILoadError: 如果找不到 abis 目录
    """
    # 从当前文件位置开始
    current = Path(__file__).resolve().parent
    
    # 在常见位置查找 abis 文件夹
    search_paths = [
        current.parent / "abis",          # 项目根目录 / abis
        current / "abis",                  # utils / abis
        Path.cwd() / "abis",               # 当前工作目录 / abis
    ]
    
    for path in search_paths:
        if path.exists() and path.is_dir():
            return path
    
    # 如果不存在则创建 abis 目录（在项目根目录）
    default_path = current.parent / "abis"
    default_path.mkdir(exist_ok=True)
    return default_path


def get_abi_path(file_name: str) -> Path:
    """
    获取 ABI 文件的完整路径
    
    参数:
        file_name: ABI 文件名（带或不带 .json 扩展名）
        
    返回:
        ABI 文件的完整路径
    """
    abis_dir = _find_abis_directory()
    
    # 确保有 .json 扩展名
    if not file_name.endswith(".json"):
        file_name = f"{file_name}.json"
    
    return abis_dir / file_name


def load_abi(
    file_name: str,
    use_cache: bool = True,
) -> List[Dict[str, Any]]:
    """
    从 abis 目录加载合约 ABI
    
    支持加载以下 ABI:
    - ERC20 代币
    - Uniswap V2/V3 路由器和工厂
    - 自定义合约
    
    参数:
        file_name: ABI 文件名（带或不带 .json 扩展名）
        use_cache: 是否使用缓存的 ABI（默认: True）
        
    返回:
        包含函数/事件定义的 ABI 列表
        
    异常:
        ABILoadError: 如果文件不存在或包含无效的 JSON
        
    示例:
        >>> abi = load_abi("erc20")
        >>> abi = load_abi("uniswap_v2_router.json")
    """
    # 规范化文件名
    if not file_name.endswith(".json"):
        file_name = f"{file_name}.json"
    
    # 检查缓存
    if use_cache and file_name in _abi_cache:
        return _abi_cache[file_name]
    
    # 获取文件路径
    abi_path = get_abi_path(file_name)
    
    if not abi_path.exists():
        raise ABILoadError(
            f"ABI 文件不存在: {abi_path}\n"
            f"请确保 ABI 文件存在于 'abis' 目录中。"
        )
    
    try:
        content = _json_load_file(abi_path)
    except (json.JSONDecodeError, ValueError) as e:
        raise ABILoadError(f"{abi_path} 中的 JSON 格式无效: {e}")
    except Exception as e:
        raise ABILoadError(f"读取 {abi_path} 失败: {e}")
    
    # 处理原始 ABI 数组和包装格式
    # 某些来源将 ABI 包装在 {"abi": [...]} 中
    if isinstance(content, dict):
        if "abi" in content:
            abi = content["abi"]
        elif "result" in content:
            # Etherscan 格式
            if isinstance(content["result"], str):
                abi = _json_loads(content["result"].encode()) if HAS_ORJSON else json.loads(content["result"])
            else:
                abi = content["result"]
        else:
            raise ABILoadError(
                f"{file_name} 中的 ABI 格式意外。"
                f"期望列表或带有 'abi' 键的字典。"
            )
    elif isinstance(content, list):
        abi = content
    else:
        raise ABILoadError(
            f"{file_name} 中的 ABI 格式无效。"
            f"期望列表或字典，得到 {type(content).__name__}"
        )
    
    # 缓存 ABI
    if use_cache:
        _abi_cache[file_name] = abi
    
    return abi


def load_abis(file_names: List[str]) -> Dict[str, List[Dict[str, Any]]]:
    """
    一次加载多个 ABI
    
    参数:
        file_names: 要加载的 ABI 文件名列表
        
    返回:
        文件名到其 ABI 的字典映射
        
    示例:
        >>> abis = load_abis(["erc20", "uniswap_v2_router"])
        >>> erc20_abi = abis["erc20"]
    """
    return {name: load_abi(name) for name in file_names}


def clear_abi_cache() -> None:
    """清除 ABI 缓存以释放内存或强制重新加载"""
    _abi_cache.clear()


def get_cached_abis() -> List[str]:
    """
    获取当前缓存的 ABI 名称列表
    
    返回:
        已缓存的 ABI 文件名列表
    """
    return list(_abi_cache.keys())


# =====================================================
# 常用 ABI 辅助函数
# =====================================================

@lru_cache(maxsize=1)
def get_erc20_abi() -> List[Dict[str, Any]]:
    """
    获取标准 ERC20 ABI
    
    如果文件不存在则返回缓存的最小 ERC20 ABI。
    """
    try:
        return load_abi("erc20")
    except ABILoadError:
        # 返回最小 ERC20 ABI 作为后备
        return [
            {
                "constant": True,
                "inputs": [],
                "name": "name",
                "outputs": [{"name": "", "type": "string"}],
                "type": "function"
            },
            {
                "constant": True,
                "inputs": [],
                "name": "symbol",
                "outputs": [{"name": "", "type": "string"}],
                "type": "function"
            },
            {
                "constant": True,
                "inputs": [],
                "name": "decimals",
                "outputs": [{"name": "", "type": "uint8"}],
                "type": "function"
            },
            {
                "constant": True,
                "inputs": [],
                "name": "totalSupply",
                "outputs": [{"name": "", "type": "uint256"}],
                "type": "function"
            },
            {
                "constant": True,
                "inputs": [{"name": "owner", "type": "address"}],
                "name": "balanceOf",
                "outputs": [{"name": "", "type": "uint256"}],
                "type": "function"
            },
            {
                "constant": True,
                "inputs": [
                    {"name": "owner", "type": "address"},
                    {"name": "spender", "type": "address"}
                ],
                "name": "allowance",
                "outputs": [{"name": "", "type": "uint256"}],
                "type": "function"
            },
            {
                "constant": False,
                "inputs": [
                    {"name": "spender", "type": "address"},
                    {"name": "value", "type": "uint256"}
                ],
                "name": "approve",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function"
            },
            {
                "constant": False,
                "inputs": [
                    {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"}
                ],
                "name": "transfer",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function"
            },
            {
                "constant": False,
                "inputs": [
                    {"name": "from", "type": "address"},
                    {"name": "to", "type": "address"},
                    {"name": "value", "type": "uint256"}
                ],
                "name": "transferFrom",
                "outputs": [{"name": "", "type": "bool"}],
                "type": "function"
            },
            {
                "anonymous": False,
                "inputs": [
                    {"indexed": True, "name": "from", "type": "address"},
                    {"indexed": True, "name": "to", "type": "address"},
                    {"indexed": False, "name": "value", "type": "uint256"}
                ],
                "name": "Transfer",
                "type": "event"
            },
            {
                "anonymous": False,
                "inputs": [
                    {"indexed": True, "name": "owner", "type": "address"},
                    {"indexed": True, "name": "spender", "type": "address"},
                    {"indexed": False, "name": "value", "type": "uint256"}
                ],
                "name": "Approval",
                "type": "event"
            }
        ]


def extract_function_selector(abi_entry: Dict[str, Any]) -> Optional[str]:
    """
    从 ABI 条目中提取 4 字节函数选择器
    
    参数:
        abi_entry: 单个 ABI 条目（函数定义）
        
    返回:
        函数选择器（0x... 格式的十六进制字符串），如果不是函数则返回 None
    """
    if abi_entry.get("type") != "function":
        return None
    
    name = abi_entry.get("name", "")
    inputs = abi_entry.get("inputs", [])
    
    # 构建签名
    input_types = ",".join(inp.get("type", "") for inp in inputs)
    signature = f"{name}({input_types})"
    
    # 在这里导入以避免循环依赖
    from web3 import Web3
    
    # 计算选择器（keccak256 的前 4 字节）
    selector = Web3.keccak(text=signature)[:4].hex()
    return f"0x{selector}"


def get_function_by_name(
    abi: List[Dict[str, Any]],
    function_name: str,
) -> Optional[Dict[str, Any]]:
    """
    通过名称在 ABI 中查找函数定义
    
    参数:
        abi: 合约 ABI
        function_name: 要查找的函数名称
        
    返回:
        函数 ABI 条目，如果未找到则返回 None
    """
    for entry in abi:
        if entry.get("type") == "function" and entry.get("name") == function_name:
            return entry
    return None
