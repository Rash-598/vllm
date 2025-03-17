"""CacheEngine class for managing the KV cache."""
from typing import Dict, List

import torch

from vllm import _vmm_ops as vmm
from vllm.attention import get_attn_backend
from vllm.config import (CacheConfig, DeviceConfig, ModelConfig,
                         ParallelConfig, SchedulerConfig)
from vllm.logger import init_logger
from vllm.utils import (STR_DTYPE_TO_TORCH_DTYPE, TORCH_DTYPE_TO_STR_DTYPE,
                        get_dtype_size, is_pin_memory_available)

_MB = 1 << 20
logger = init_logger(__name__)


class CacheEngineVMM:
    """Manages the KV cache use VMM cuda api.

    This class is responsible for initializing and managing the GPU and CPU KV
    caches. It also provides methods for performing KV cache operations, such
    as swapping and copying.
    Assign B: batch size, S: sequence length, L: num_layers,
           H: num_kv_heads, D: head_size
    For key/value cache, all layers will be packed in one cache space.
    Specifically, it will reserve 2 cache spaces, the size of each is
    B * S * L * H * D * dtype_size
    """

    def __init__(
        self,
        cache_config: CacheConfig,
        model_config: ModelConfig,
        parallel_config: ParallelConfig,
        scheduler_config: SchedulerConfig,
        device_config: DeviceConfig,
    ) -> None:
        self.cache_config = cache_config
        self.model_config = model_config
        self.parallel_config = parallel_config
        self.scheduler_config = scheduler_config
        self.device_config = device_config

        if self.device_config.device_type != "cuda":
            raise RuntimeError("VMM only support cuda device.")

        self.head_size = model_config.get_head_size()
        self.num_layers = model_config.get_num_layers(parallel_config)
        self.num_kv_heads = model_config.get_num_kv_heads(parallel_config)

        self.block_size = cache_config.block_size
        self.block_bytes_size = self.cache_config.block_bytes_size
        self.num_gpu_blocks = cache_config.num_gpu_blocks
        self.num_cpu_blocks = cache_config.num_cpu_blocks

        if cache_config.cache_dtype == "auto":
            self.dtype = model_config.dtype
        else:
            self.dtype = STR_DTYPE_TO_TORCH_DTYPE[cache_config.cache_dtype]

        self.dtype_size = get_dtype_size(self.dtype)
        self.max_batch_size = self.scheduler_config.max_num_seqs
        self.max_seq_len = self.scheduler_config.max_model_len
        # If max_seq_len is not divisible by block_size,
        # round up to the nearest value that is.
        if self.max_seq_len % self.block_size != 0:
            self.max_seq_len = ((self.max_seq_len // self.block_size + 1) *
                                self.block_size)
            logger.warning(
                "self.max_seq_len mod self.block_size != 0, "
                "round up max_seq_len to %d", self.max_seq_len)

        self.token_size = self.num_kv_heads * self.head_size
        self.sequence_buffer_size = self.max_seq_len * self.token_size
        self.sequence_buffer_bytes_size = (self.sequence_buffer_size *
                                           self.dtype_size)

        self.cache_space_size = self.num_layers * self.sequence_buffer_size
        self.cache_sapce_bytes_size = self.cache_space_size * self.dtype_size

        assert self.cache_sapce_bytes_size % self.block_bytes_size == 0, \
            "cache_sapce_bytes_size must be divisible by block_bytes_size"

        self.cache_space_page_num = (self.cache_sapce_bytes_size //
                                     self.block_bytes_size)

        logger.info(
            "CacheEngineVMM basic info: { block_size: %d, block_bytes_size: %d dtype_size: %d, "
            "head_size: %d, num_kv_heads: %d ,max_seq_len: %d, "
            "max_batch_size: %d, num_layers: %d, token_size: %d, "
            "sequence_buffer_size: %d, cache_space_size: %d, "
            "cache_sapce_bytes_size: %d, cache_space_page_num: %d }",
            self.block_size, self.block_bytes_size, self.dtype_size, self.head_size,
            self.num_kv_heads, self.max_seq_len, self.max_batch_size,
            self.num_layers, self.token_size, self.sequence_buffer_size,
            self.cache_space_size, self.cache_sapce_bytes_size,
            self.cache_space_page_num)

        self.device_cache_allocator = vmm.CacheAllocator()

        page_size = self.block_bytes_size // (2 * _MB)
        if page_size > 1:
            logger.info("VMM: Set page size to %d MB", page_size * 2)
            self.device_cache_allocator.set_page_size(page_size)
        # record the allocated handles for each buffer in a cache space
        self.allocated_block_counts = [0 for _ in range(self.max_batch_size)]

        # Get attention backend.
        self.attn_backend = get_attn_backend(
            head_size=self.head_size,
            dtype=model_config.dtype,
            kv_cache_dtype=cache_config.cache_dtype,
            block_size=self.block_size,
            is_attention_free=model_config.is_attention_free,
            use_mla=model_config.use_mla,
        )
        assert self.attn_backend.get_name() == "FLASH_ATTN"

        # Initialize the cache.
        logger.info(f"Before Free GPU memory: {torch.cuda.mem_get_info()}")
        self.gpu_cache_ptr = self._reserve_gpu_kv_cache()
        self.gpu_cache = self._init_gpu_kv_cache_tensor()
        logger.info(f"VMM Shape of GPU cache: {len(self.gpu_cache)}, {len(self.gpu_cache[0])}, {self.gpu_cache[0][0].shape}")
        logger.info(f"After Free GPU memory: {torch.cuda.mem_get_info()}")
        
        # TODO: Implement CPU cache and swap
        # self.cpu_cache = self._allocate_kv_cache(self.num_cpu_blocks, "cpu")

    def _reserve_gpu_kv_cache(self) -> List[vmm.CacheDevicePtr]:
        key_ptr = vmm.CacheDevicePtr()
        value_ptr = vmm.CacheDevicePtr()

        if (self.device_cache_allocator.reserve_cache_ptr(
                key_ptr, self.max_batch_size * self.cache_space_page_num) !=
                0):
            raise RuntimeError("Failed to reserve key cache ptr.")

        if (self.device_cache_allocator.reserve_cache_ptr(
                value_ptr, self.max_batch_size * self.cache_space_page_num) !=
                0):
            raise RuntimeError("Failed to reserve value cache ptr.")

        return [key_ptr, value_ptr]

    def _init_gpu_kv_cache_tensor(self) -> List[List[torch.Tensor]]:
        # We have to allocate one block for each ptr, otherwise wrap to tensor
        # will fail, here we allocate one block for each CacheDevicePtr
        alloc_dict = {}
        for i in range(self.max_batch_size):
            alloc_dict[i] = 1
        self.alloc_seqs(alloc_dict)

        key_cache_ptr = self.gpu_cache_ptr[0]
        value_cache_ptr = self.gpu_cache_ptr[1]

        shape = (self.max_batch_size, self.max_seq_len, self.num_layers,
                 self.num_kv_heads, self.head_size)
        dtype = TORCH_DTYPE_TO_STR_DTYPE[self.dtype]
        key_cache_tensor: torch.Tensor = vmm.wrap_cache_ptr_to_tensor(
            key_cache_ptr, dtype, shape)
        value_cache_tensor: torch.Tensor = vmm.wrap_cache_ptr_to_tensor(
            value_cache_ptr, dtype, shape)

        kv_cache_views = [[
            key_cache_tensor[:, :, i, :, :], value_cache_tensor[:, :, i, :, :]
        ] for i in range(self.num_layers)]
        return kv_cache_views

    def _allocate_kv_cache(
        self,
        num_blocks: int,
        device: str = 'cpu',
    ) -> List[torch.Tensor]:
        """Allocates KV cache on the specified device."""
        kv_cache_shape = self.attn_backend.get_kv_cache_shape(
            num_blocks, self.block_size, self.num_kv_heads, self.head_size)
        pin_memory = is_pin_memory_available() if device == "cpu" else False
        kv_cache: List[torch.Tensor] = []
        for _ in range(self.num_layers):
            # null block in CpuGpuBlockAllocator requires at least that
            # block to be zeroed-out. We zero-out everything for simplicity.
            kv_cache.append(
                torch.zeros(kv_cache_shape,
                            dtype=self.dtype,
                            pin_memory=pin_memory,
                            device=device))
        return kv_cache

    def swap_in(self, src_to_dst: torch.Tensor) -> None:
        raise NotImplementedError("swap_in is not implemented for VMM now.")

    def swap_out(self, src_to_dst: torch.Tensor) -> None:
        raise NotImplementedError("swap_out is not implemented for VMM now.")

    def copy(self, src_to_dsts: torch.Tensor) -> None:
        self.attn_backend.copy_blocks(self.gpu_cache, src_to_dsts)

    @staticmethod
    def get_cache_block_size(
        cache_config: CacheConfig,
        model_config: ModelConfig,
        parallel_config: ParallelConfig,
    ) -> int:
        # single block bytes size * 2 (key and value)
        return (cache_config.block_bytes_size * 2)

    def alloc_seqs(self, allocated_block_counts: Dict[int, int]) -> None:
        """Allocate cache handles for the given number of blocks."""
        for buffer_id, num_blocks in allocated_block_counts.items():
            allocated_blocks = self.allocated_block_counts[buffer_id]

            num_blocks -= allocated_blocks
            start_offset = buffer_id * self.cache_sapce_bytes_size
            if num_blocks > 0:
                allocated_blocks = self.allocated_block_counts[buffer_id]
                offset = (start_offset +
                          allocated_blocks * self.block_bytes_size)
                self.alloc_one_seq(buffer_id, num_blocks, offset)
                # logger.info(
                #     "VMM Alloc: buffer_id: %d, num_blocks: %d, "
                #     "allocated_block_counts: %s", buffer_id, num_blocks,
                #     str(self.allocated_block_counts))

    def alloc_one_seq(self,
                      buffer_id: int,
                      num_blocks: int = 1,
                      offset: int = 0) -> None:
        """Allocate cache handles for the given number of blocks."""
        key_cache_ptr = self.gpu_cache_ptr[0]
        value_cache_ptr = self.gpu_cache_ptr[1]

        status1 = self.device_cache_allocator.alloc_cache_ptr(
            key_cache_ptr, num_blocks, offset)
        status2 = self.device_cache_allocator.alloc_cache_ptr(
            value_cache_ptr, num_blocks, offset)
        if status1 != 0 or status2 != 0:
            logger.error(
                "VMM Alloc: buffer_id: %d, num_blocks: %d, offset: %d",
                buffer_id, num_blocks, offset)
            raise RuntimeError(f"Failed to allocate cache handles. "
                               f"status1: {status1}, status2: {status2}")

        self.allocated_block_counts[buffer_id] += num_blocks

    def free_seqs(self, free_buffer_ids: List[int]) -> None:
        """Free cache handles for the given buffer ids."""
        for buffer_id in free_buffer_ids:
            num_blocks = self.allocated_block_counts[buffer_id]
            offset = buffer_id * self.cache_sapce_bytes_size
            self.free_one_seq(buffer_id, num_blocks, offset)

    def free_one_seq(self,
                     buffer_id: int,
                     num_blocks: int = 0,
                     offset: int = 0) -> None:
        """Free cache handles for the given buffer id."""
        key_cache_ptr = self.gpu_cache_ptr[0]
        value_cache_ptr = self.gpu_cache_ptr[1]

        status1 = self.device_cache_allocator.release_cache_ptr(
            key_cache_ptr, num_blocks, offset)
        status2 = self.device_cache_allocator.release_cache_ptr(
            value_cache_ptr, num_blocks, offset)
        if status1 != 0 or status2 != 0:
            logger.error("VMM Free: buffer_id: %d, num_blocks: %d, offset: %d",
                         buffer_id, num_blocks, offset)
            raise RuntimeError(f"Failed to free cache handles. "
                               f"status1: {status1}, status2: {status2}")

        self.allocated_block_counts[buffer_id] -= num_blocks
