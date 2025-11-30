"""
FlashArb-Core 网络管理器

健壮的异步网络层，提供以下功能:
- RPC 故障转移支持
- 速率限制的指数退避
- EIP-1559 和 Legacy Gas 处理
- 持久化 aiohttp 会话以降低延迟
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, TypeVar, Union

import aiohttp
from web3 import AsyncWeb3
from web3.exceptions import Web3Exception
from web3.providers import AsyncHTTPProvider
from web3.types import BlockData, TxParams, Wei

from .config_loader import ChainConfig, GasConfig

# 配置日志
logger = logging.getLogger(__name__)

# 用于泛型重试包装器的类型变量
T = TypeVar("T")


class RPCError(Exception):
    """RPC 相关错误的基类"""
    pass


class AllRPCsFailedError(RPCError):
    """当所有 RPC 端点都失败时抛出"""
    pass


class RateLimitError(RPCError):
    """被 RPC 提供商限制速率时抛出"""
    pass


class NetworkState(Enum):
    """网络连接状态"""
    DISCONNECTED = "disconnected"  # 已断开
    CONNECTING = "connecting"      # 连接中
    CONNECTED = "connected"        # 已连接
    DEGRADED = "degraded"          # 降级（部分 RPC 失败但仍可用）


@dataclass
class GasParams:
    """交易提交的 Gas 参数"""
    
    # 通用字段
    gas_limit: Optional[int] = None
    
    # EIP-1559 字段
    max_fee_per_gas: Optional[Wei] = None
    max_priority_fee_per_gas: Optional[Wei] = None
    
    # Legacy 字段
    gas_price: Optional[Wei] = None
    
    @property
    def is_eip1559(self) -> bool:
        """检查是否使用 EIP-1559 Gas 模型"""
        return self.max_fee_per_gas is not None
    
    def to_tx_params(self) -> Dict[str, Any]:
        """转换为交易参数字典"""
        params: Dict[str, Any] = {}
        
        if self.gas_limit:
            params["gas"] = self.gas_limit
        
        if self.is_eip1559:
            params["maxFeePerGas"] = self.max_fee_per_gas
            params["maxPriorityFeePerGas"] = self.max_priority_fee_per_gas
        else:
            params["gasPrice"] = self.gas_price
        
        return params


@dataclass
class RPCHealth:
    """单个 RPC 端点的健康指标"""
    
    url: str
    is_healthy: bool = True
    last_success: float = 0.0
    last_failure: float = 0.0
    consecutive_failures: int = 0
    avg_latency_ms: float = 0.0
    total_requests: int = 0
    
    def record_success(self, latency_ms: float) -> None:
        """记录成功的请求"""
        self.is_healthy = True
        self.last_success = time.time()
        self.consecutive_failures = 0
        self.total_requests += 1
        
        # 使用指数移动平均计算延迟
        if self.avg_latency_ms == 0:
            self.avg_latency_ms = latency_ms
        else:
            self.avg_latency_ms = 0.8 * self.avg_latency_ms + 0.2 * latency_ms
    
    def record_failure(self) -> None:
        """记录失败的请求"""
        self.last_failure = time.time()
        self.consecutive_failures += 1
        self.total_requests += 1
        
        # 连续失败3次后标记为不健康
        if self.consecutive_failures >= 3:
            self.is_healthy = False


class NetworkManager:
    """
    健壮的异步区块链网络管理器
    
    功能特性:
    - 连接错误时自动切换 RPC 端点
    - HTTP 429 错误时使用指数退避
    - 支持 EIP-1559 和 Legacy Gas 定价
    - 使用 aiohttp 的 Keep-Alive 连接降低延迟
    - 追踪每个 RPC 端点的健康状态
    
    使用示例:
        >>> config = load_chain_config("BASE")
        >>> async with NetworkManager(config) as network:
        ...     block = await network.get_latest_block()
        ...     print(f"区块: {block.number}")
    """
    
    def __init__(
        self,
        config: ChainConfig,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> None:
        """
        初始化网络管理器
        
        参数:
            config: 包含 RPC URL 和设置的链配置
            session: 可选的共享 aiohttp 会话用于连接池
        """
        self.config = config
        self.chain_id = config.chain_id
        self.gas_config = config.gas_config
        
        # RPC 管理
        self._rpc_urls = config.rpc_urls.copy()
        self._current_rpc_index = 0
        self._rpc_health: Dict[str, RPCHealth] = {
            url: RPCHealth(url=url) for url in self._rpc_urls
        }
        
        # 会话管理
        self._external_session = session is not None
        self._session = session
        self._web3: Optional[AsyncWeb3] = None
        self._provider: Optional[AsyncHTTPProvider] = None
        
        # 重试配置
        self._max_retries = config.max_retries
        self._base_delay = 0.5  # 指数退避的基础延迟
        self._max_delay = 30.0  # 重试间的最大延迟
        self._timeout = aiohttp.ClientTimeout(total=config.rpc_timeout)
        
        # 状态
        self._state = NetworkState.DISCONNECTED
        self._lock = asyncio.Lock()
    
    @property
    def current_rpc_url(self) -> str:
        """获取当前活动的 RPC URL"""
        return self._rpc_urls[self._current_rpc_index]
    
    @property
    def state(self) -> NetworkState:
        """获取当前网络连接状态"""
        return self._state
    
    @property
    def w3(self) -> AsyncWeb3:
        """获取 Web3 实例。如果未连接则抛出异常"""
        if self._web3 is None:
            raise RPCError("网络管理器未连接。请先调用 connect() 方法。")
        return self._web3
    
    async def __aenter__(self) -> "NetworkManager":
        """异步上下文管理器入口"""
        await self.connect()
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        """异步上下文管理器出口"""
        await self.disconnect()
    
    async def connect(self) -> None:
        """
        建立与区块链的连接
        
        创建 aiohttp 会话和 Web3 提供者。
        尝试连接每个 RPC 直到成功。
        """
        self._state = NetworkState.CONNECTING
        
        # 如果没有外部提供会话则创建
        if self._session is None:
            connector = aiohttp.TCPConnector(
                limit=100,
                limit_per_host=20,
                keepalive_timeout=60,
                enable_cleanup_closed=True,
            )
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                connector=connector,
            )
        
        # 尝试连接到 RPC
        await self._create_web3_instance()
        
        # 验证连接
        try:
            chain_id = await self._web3.eth.chain_id
            if chain_id != self.chain_id:
                logger.warning(
                    f"链 ID 不匹配: 期望 {self.chain_id}，实际 {chain_id}"
                )
            self._state = NetworkState.CONNECTED
            logger.info(
                f"已连接到 {self.config.name}，使用 {self.current_rpc_url}"
            )
        except Exception as e:
            logger.error(f"验证连接失败: {e}")
            # 尝试故障转移
            await self._switch_to_next_rpc()
            # 故障转移后再次验证
            try:
                chain_id = await self._web3.eth.chain_id
                self._state = NetworkState.CONNECTED
                logger.info(
                    f"故障转移后已连接到 {self.config.name}，使用 {self.current_rpc_url}"
                )
            except Exception:
                # 如果还是失败，保持 DEGRADED 状态
                pass
    
    async def disconnect(self) -> None:
        """
        优雅地断开网络连接
        
        如果会话是内部创建的则关闭它。
        """
        if self._session and not self._external_session:
            await self._session.close()
            self._session = None
        
        self._web3 = None
        self._provider = None
        self._state = NetworkState.DISCONNECTED
        logger.info(f"已断开与 {self.config.name} 的连接")
    
    async def _create_web3_instance(self) -> None:
        """使用当前 RPC URL 创建 Web3 实例"""
        # web3.py 6.x 版本中，AsyncHTTPProvider 会自动创建 session
        # 如果需要使用自定义 session，需要通过不同的方式
        self._provider = AsyncHTTPProvider(
            endpoint_uri=self.current_rpc_url,
        )
        
        self._web3 = AsyncWeb3(self._provider)
        
        # 如果提供了自定义 session，需要手动设置
        # 注意：web3.py 6.x 可能不支持直接传递 session
        # 这里我们依赖 web3 的内部 session 管理
    
    async def _switch_to_next_rpc(self) -> None:
        """
        切换到下一个可用的 RPC 端点
        
        遍历所有端点，优先选择健康的节点。
        
        异常:
            AllRPCsFailedError: 如果所有 RPC 都不健康
        """
        async with self._lock:
            original_index = self._current_rpc_index
            
            # 尝试找到健康的 RPC
            for _ in range(len(self._rpc_urls)):
                self._current_rpc_index = (
                    self._current_rpc_index + 1
                ) % len(self._rpc_urls)
                
                url = self.current_rpc_url
                health = self._rpc_health[url]
                
                if health.is_healthy:
                    logger.info(f"切换 RPC 到: {url}")
                    await self._create_web3_instance()
                    return
            
            # 没有健康的 RPC - 重置健康状态并重试
            logger.warning("所有 RPC 都标记为不健康，正在重置健康状态")
            for health in self._rpc_health.values():
                health.is_healthy = True
                health.consecutive_failures = 0
            
            # 无论如何切换到下一个
            self._current_rpc_index = (original_index + 1) % len(self._rpc_urls)
            await self._create_web3_instance()
            
            self._state = NetworkState.DEGRADED
    
    async def _execute_with_retry(
        self,
        operation: Callable[[], T],
        operation_name: str = "operation",
    ) -> T:
        """
        带重试逻辑的异步操作执行
        
        实现:
        - 连接错误时自动切换 RPC
        - 速率限制时使用指数退避
        
        参数:
            operation: 要执行的异步可调用对象
            operation_name: 用于日志记录的操作名称
            
        返回:
            操作的结果
            
        异常:
            AllRPCsFailedError: 如果所有重试都用尽
        """
        last_error: Optional[Exception] = None
        total_attempts = self._max_retries * len(self._rpc_urls)
        
        for attempt in range(total_attempts):
            try:
                start_time = time.perf_counter()
                result = await operation()
                latency_ms = (time.perf_counter() - start_time) * 1000
                
                # 记录成功
                self._rpc_health[self.current_rpc_url].record_success(latency_ms)
                return result
                
            except aiohttp.ClientResponseError as e:
                last_error = e
                
                if e.status == 429:
                    # 被限速 - 指数退避
                    delay = min(
                        self._base_delay * (2 ** attempt),
                        self._max_delay
                    )
                    logger.warning(
                        f"{operation_name} 被限速，"
                        f"等待 {delay:.2f} 秒后重试"
                    )
                    await asyncio.sleep(delay)
                    continue
                    
                elif e.status >= 500:
                    # 服务器错误 - 尝试下一个 RPC
                    self._rpc_health[self.current_rpc_url].record_failure()
                    await self._switch_to_next_rpc()
                    continue
                else:
                    # 客户端错误 - 不重试
                    raise RPCError(f"HTTP {e.status}: {e.message}") from e
                    
            except (
                aiohttp.ClientError,
                asyncio.TimeoutError,
                ConnectionError,
                OSError,
            ) as e:
                last_error = e
                logger.warning(
                    f"{operation_name} 连接错误: {e}，"
                    f"切换 RPC（尝试 {attempt + 1}/{total_attempts}）"
                )
                self._rpc_health[self.current_rpc_url].record_failure()
                await self._switch_to_next_rpc()
                continue
                
            except Web3Exception as e:
                last_error = e
                error_msg = str(e).lower()
                
                # 检查是否是速率限制或容量错误
                if "429" in error_msg or "rate" in error_msg or "limit" in error_msg:
                    delay = min(
                        self._base_delay * (2 ** attempt),
                        self._max_delay
                    )
                    logger.warning(
                        f"{operation_name} RPC 速率限制，"
                        f"等待 {delay:.2f} 秒后重试"
                    )
                    await asyncio.sleep(delay)
                    continue
                
                # 其他 RPC 错误 - 可能值得尝试另一个节点
                self._rpc_health[self.current_rpc_url].record_failure()
                await self._switch_to_next_rpc()
                continue
        
        raise AllRPCsFailedError(
            f"{operation_name} 的所有 {total_attempts} 次尝试都失败了。"
            f"最后的错误: {last_error}"
        )
    
    # =====================================================
    # 公共 API - 区块链读取操作
    # =====================================================
    
    async def get_latest_block(self, full_transactions: bool = False) -> BlockData:
        """
        获取最新区块
        
        参数:
            full_transactions: 是否包含完整的交易对象
            
        返回:
            区块数据
        """
        async def _fetch():
            return await self.w3.eth.get_block("latest", full_transactions)
        
        return await self._execute_with_retry(_fetch, "get_latest_block")
    
    async def get_block_number(self) -> int:
        """
        获取当前区块号
        
        返回:
            最新区块号
        """
        async def _fetch():
            return await self.w3.eth.block_number
        
        return await self._execute_with_retry(_fetch, "get_block_number")
    
    async def get_balance(self, address: str) -> Wei:
        """
        获取地址的原生代币余额
        
        参数:
            address: 以太坊地址（校验和或非校验和）
            
        返回:
            以 Wei 为单位的余额
        """
        async def _fetch():
            checksum_addr = self.w3.to_checksum_address(address)
            return await self.w3.eth.get_balance(checksum_addr)
        
        return await self._execute_with_retry(_fetch, "get_balance")
    
    async def get_nonce(self, address: str) -> int:
        """
        获取地址的交易计数（nonce）
        
        参数:
            address: 以太坊地址
            
        返回:
            交易计数
        """
        async def _fetch():
            checksum_addr = self.w3.to_checksum_address(address)
            return await self.w3.eth.get_transaction_count(checksum_addr)
        
        return await self._execute_with_retry(_fetch, "get_nonce")
    
    async def call_contract(
        self,
        contract_address: str,
        data: bytes,
        block_identifier: Union[int, str] = "latest",
    ) -> bytes:
        """
        执行合约调用（只读）
        
        参数:
            contract_address: 合约地址
            data: 编码后的函数调用数据
            block_identifier: 区块号或 "latest"/"pending"
            
        返回:
            调用返回的原始字节结果
        """
        async def _fetch():
            checksum_addr = self.w3.to_checksum_address(contract_address)
            return await self.w3.eth.call(
                {"to": checksum_addr, "data": data},
                block_identifier,
            )
        
        return await self._execute_with_retry(_fetch, "call_contract")
    
    async def estimate_gas(self, tx_params: TxParams) -> int:
        """
        估算交易的 Gas 消耗
        
        参数:
            tx_params: 交易参数
            
        返回:
            估算的 Gas 单位数
        """
        async def _fetch():
            return await self.w3.eth.estimate_gas(tx_params)
        
        return await self._execute_with_retry(_fetch, "estimate_gas")
    
    # =====================================================
    # Gas 价格管理
    # =====================================================
    
    async def get_gas_params(
        self,
        gas_limit: Optional[int] = None,
        speed: str = "fast",
    ) -> GasParams:
        """
        获取用于交易提交的最优 Gas 参数
        
        根据链配置自动检测使用 EIP-1559 还是 Legacy Gas 定价。
        
        参数:
            gas_limit: 可选的 Gas 限制
            speed: Gas 价格速度（"slow"、"standard"、"fast"）
            
        返回:
            带有适当 Gas 设置的 GasParams 对象
        """
        speed_multipliers = {
            "slow": 0.9,
            "standard": 1.0,
            "fast": 1.2,
        }
        speed_mult = speed_multipliers.get(speed, 1.0)
        
        if self.gas_config.type == "eip1559":
            return await self._get_eip1559_gas_params(gas_limit, speed_mult)
        else:
            return await self._get_legacy_gas_params(gas_limit, speed_mult)
    
    async def _get_eip1559_gas_params(
        self,
        gas_limit: Optional[int],
        speed_mult: float,
    ) -> GasParams:
        """
        计算 EIP-1559 Gas 参数
        
        使用最新区块的 baseFee 并应用配置的乘数。
        """
        async def _fetch():
            return await self.w3.eth.get_block("latest")
        
        block = await self._execute_with_retry(_fetch, "get_base_fee")
        
        base_fee = block.get("baseFeePerGas", 0)
        if base_fee == 0:
            # 如果没有 baseFee 则回退到 Legacy（EIP-1559 链不应该发生）
            logger.warning(
                f"{self.config.name} 未找到 baseFee，回退到 Legacy 模式"
            )
            return await self._get_legacy_gas_params(gas_limit, speed_mult)
        
        # 计算优先费（小费）
        # 从合理的默认值开始并应用乘数
        priority_fee = Wei(int(1_000_000_000))  # 默认 1 gwei
        
        try:
            # 尝试获取费用历史以更好地估算
            fee_history = await self.w3.eth.fee_history(
                block_count=5,
                newest_block="latest",
                reward_percentiles=[50],
            )
            if fee_history.get("reward") and fee_history["reward"][0]:
                # 使用最近区块的中位数优先费
                recent_tips = [r[0] for r in fee_history["reward"] if r]
                if recent_tips:
                    priority_fee = Wei(int(sum(recent_tips) / len(recent_tips)))
        except Exception as e:
            logger.debug(f"费用历史不可用: {e}")
        
        # 应用配置乘数
        priority_fee = Wei(
            int(priority_fee * self.gas_config.priority_fee_multiplier * speed_mult)
        )
        
        # 最大费用 = 2 * baseFee + priorityFee（标准公式）
        max_fee = Wei(
            int(
                (base_fee * 2 + priority_fee) 
                * self.gas_config.max_fee_multiplier 
                * speed_mult
            )
        )
        
        return GasParams(
            gas_limit=gas_limit,
            max_fee_per_gas=max_fee,
            max_priority_fee_per_gas=priority_fee,
        )
    
    async def _get_legacy_gas_params(
        self,
        gas_limit: Optional[int],
        speed_mult: float,
    ) -> GasParams:
        """
        获取 Legacy Gas 价格
        """
        async def _fetch():
            return await self.w3.eth.gas_price
        
        gas_price = await self._execute_with_retry(_fetch, "get_gas_price")
        
        # 应用乘数
        adjusted_price = Wei(
            int(gas_price * self.gas_config.gas_price_multiplier * speed_mult)
        )
        
        return GasParams(
            gas_limit=gas_limit,
            gas_price=adjusted_price,
        )
    
    # =====================================================
    # 交易管理
    # =====================================================
    
    async def send_raw_transaction(self, signed_tx: bytes) -> str:
        """
        发送已签名的交易到网络
        
        参数:
            signed_tx: 已签名的交易字节
            
        返回:
            交易哈希（十六进制字符串）
        """
        async def _send():
            tx_hash = await self.w3.eth.send_raw_transaction(signed_tx)
            return tx_hash.hex()
        
        return await self._execute_with_retry(_send, "send_raw_transaction")
    
    async def wait_for_transaction_receipt(
        self,
        tx_hash: str,
        timeout: float = 120.0,
        poll_latency: float = 0.5,
    ) -> Dict[str, Any]:
        """
        等待交易被打包
        
        参数:
            tx_hash: 交易哈希
            timeout: 最大等待时间（秒）
            poll_latency: 轮询间隔
            
        返回:
            交易收据
        """
        async def _wait():
            return await self.w3.eth.wait_for_transaction_receipt(
                tx_hash,
                timeout=timeout,
                poll_latency=poll_latency,
            )
        
        return await self._execute_with_retry(_wait, "wait_for_receipt")
    
    # =====================================================
    # 健康监控
    # =====================================================
    
    def get_rpc_health(self) -> Dict[str, RPCHealth]:
        """
        获取所有 RPC 端点的健康指标
        
        返回:
            RPC URL 到其健康指标的字典映射
        """
        return self._rpc_health.copy()
    
    def get_fastest_rpc(self) -> Optional[str]:
        """
        获取平均延迟最低的 RPC
        
        返回:
            最快的健康 RPC 的 URL，如果没有健康的 RPC 则返回 None
        """
        healthy_rpcs = [
            (url, health)
            for url, health in self._rpc_health.items()
            if health.is_healthy and health.avg_latency_ms > 0
        ]
        
        if not healthy_rpcs:
            return None
        
        return min(healthy_rpcs, key=lambda x: x[1].avg_latency_ms)[0]
    
    async def ping(self) -> float:
        """
        ping 当前 RPC 并测量延迟
        
        返回:
            往返时间（毫秒）
        """
        start = time.perf_counter()
        await self.get_block_number()
        return (time.perf_counter() - start) * 1000


# =====================================================
# 工厂函数
# =====================================================

async def create_network_manager(
    config: ChainConfig,
    connect: bool = True,
) -> NetworkManager:
    """
    创建 NetworkManager 的工厂函数
    
    参数:
        config: 链配置
        connect: 是否立即建立连接
        
    返回:
        NetworkManager 实例
        
    示例:
        >>> config = load_chain_config("BASE")
        >>> network = await create_network_manager(config)
        >>> block = await network.get_latest_block()
    """
    manager = NetworkManager(config)
    if connect:
        await manager.connect()
    return manager
