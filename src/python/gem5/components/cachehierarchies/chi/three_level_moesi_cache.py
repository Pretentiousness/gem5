from itertools import chain
from typing import (
    List,
    Optional,
)

from m5.objects import (
    NULL,
    RubyPortProxy,
    RubySequencer,
    RubySystem,
    SubSystem,
)

from gem5.coherence_protocol import CoherenceProtocol
from gem5.utils.requires import requires

requires(coherence_protocol_required=CoherenceProtocol.CHI)

from gem5.components.boards.abstract_board import AbstractBoard
from gem5.components.cachehierarchies.abstract_cache_hierarchy import (
    AbstractCacheHierarchy,
)
from gem5.components.cachehierarchies.ruby.abstract_ruby_cache_hierarchy import (
    AbstractRubyCacheHierarchy,
)
from gem5.components.cachehierarchies.ruby.topologies.simple_pt2pt import (
    SimplePt2Pt,
)
from gem5.components.processors.abstract_core import AbstractCore
from gem5.isas import ISA
from gem5.utils.override import overrides

from .nodes.directory import SimpleDirectory
from .nodes.dma_requestor import DMARequestor
from .nodes.memory_controller import MemoryController
from .nodes.private_l1_moesi_cache_optimised import (
    PrivateL1MOESICacheOptimised,
)
from .nodes.private_l2_moesi_cache import PrivateL2MOESICache
from .nodes.shared_l3_moesi_cache import SharedL3MOESICache


class ThreeLevelMOESICacheHierarchy(AbstractRubyCacheHierarchy):
    """A three-level CHI cache hierarchy

    This hierarchy supports:
    - Private L1I/L1D caches per core
    - Private L2 caches per core
    - Shared L3 caches
    - Single directory (HNF node) no cache
    - Multiple memory controllers
    - DMA controllers
    - Configurable topology
    """

    def __init__(
        self,
        l1i_size: str = "32KiB",
        l1i_assoc: int = 4,
        l1d_size: str = "32KiB",
        l1d_assoc: int = 4,
        l2_size: str = "256KiB",
        l2_assoc: int = 8,
        l3_size: str = "2MiB",
        l3_assoc: int = 16,
        num_l3_banks: Optional[int] = None,
        enable_prefetchers: bool = False,
        topology: str = "pt2pt",
    ) -> None:
        """
        :param l1i_size: Size of L1 instruction caches
        :param l1i_assoc: Associativity of L1 instruction caches
        :param l1d_size: Size of L1 data caches
        :param l1d_assoc: Associativity of L1 data caches
        :param l2_size: Size of private L2 caches
        :param l2_assoc: Associativity of private L2 caches
        :param l3_size: Size of shared L3 cache banks
        :param l3_assoc: Associativity of shared L3 cache banks
        :param num_l3_banks: Number of L3 banks (defaults to num cores)
        :param enable_prefetchers: Enable hardware prefetchers
        :param topology: Network topology ("pt2pt", "mesh", "crossbar")
        """
        super().__init__()

        self._l1i_size = l1i_size
        self._l1i_assoc = l1i_assoc
        self._l1d_size = l1d_size
        self._l1d_assoc = l1d_assoc
        self._l2_size = l2_size
        self._l2_assoc = l2_assoc
        self._l3_size = l3_size
        self._l3_assoc = l3_assoc
        self._num_l3_banks = num_l3_banks
        self._enable_prefetchers = enable_prefetchers
        self._topology = topology

    @overrides(AbstractCacheHierarchy)
    def get_coherence_protocol(self):
        return CoherenceProtocol.CHI

    @overrides(AbstractCacheHierarchy)
    def incorporate_cache(self, board: AbstractBoard) -> None:
        super().incorporate_cache(board)

        # Initialize Ruby system
        self.ruby_system = RubySystem()

        # Set up network topology
        self._setup_network(board)

        # Create a single centralized directory
        self.directory = SimpleDirectory(
            self.ruby_system.network,
            cache_line_size=board.get_cache_line_size(),
            clk_domain=board.get_clock_domain(),
        )
        self.directory.ruby_system = self.ruby_system

        # Create core clusters (L1 + L2)
        self.core_clusters = [
            self._create_core_cluster(core, i, board)
            for i, core in enumerate(board.get_processor().get_cores())
        ]

        # Create Shared L3 cache banks
        self.l3_banks = self._create_l3_banks(board)

        # Create memory controllers (SNF nodes)
        self.memory_controllers = self._create_memory_controllers(board)

        # Create DMA controllers if needed
        if board.has_dma_ports():
            self.dma_controllers = self._create_dma_controllers(board)
        else:
            self.dma_controllers = []

        # Set up downstream routing
        self._setup_routing()

        # Count sequencers
        num_sequencers = len(self.core_clusters) * 3  # L1I + L1D + L2 per core
        if board.has_dma_ports():
            num_sequencers += len(self.dma_controllers)
        self.ruby_system.num_of_sequencers = num_sequencers

        # Connect all controllers to network
        self._connect_controllers()

        # Set up system port proxy
        self.ruby_system.sys_port_proxy = RubyPortProxy(
            ruby_system=self.ruby_system
        )
        board.connect_system_port(self.ruby_system.sys_port_proxy.in_ports)

    # @overrides(AbstractRubyCacheHierarchy)
    # def is_ruby(self) -> bool:
    #     """Indicates that this is a Ruby-based cache hierarchy"""
    #     return True

    def _setup_network(self, board: AbstractBoard) -> None:
        """Configure the network topology. We are using a custom topology."""
        if self._topology == "pt2pt":
            self.ruby_system.network = SimplePt2Pt(self.ruby_system)
        # elif self._topology == "mesh":
        #     from gem5.components.cachehierarchies.ruby.topologies.simple_mesh import SimpleMesh
        #     self.ruby_system.network = SimpleMesh(self.ruby_system)
        # elif self._topology == "crossbar":
        #     from gem5.components.cachehierarchies.ruby.topologies.crossbar import Crossbar
        #     self.ruby_system.network = Crossbar(self.ruby_system)
        else:
            raise ValueError(f"Unsupported topology: {self._topology}")

        # CHI uses 4 virtual networks: request, snoop, response, data
        self.ruby_system.number_of_virtual_networks = 4
        self.ruby_system.network.number_of_virtual_networks = 4

    def _create_core_cluster(
        self, core: AbstractCore, core_num: int, board: AbstractBoard
    ) -> SubSystem:
        """Create a core cluster with split L1 and private L2 caches"""
        cluster = SubSystem()

        # Create L1 instruction cache
        cluster.icache = PrivateL1MOESICacheOptimised(
            size=self._l1i_size,
            assoc=self._l1i_assoc,
            network=self.ruby_system.network,
            core=core,
            cache_line_size=board.get_cache_line_size(),
            target_isa=board.get_processor().get_isa(),
            clk_domain=board.get_clock_domain(),
            is_icache=True,
            enable_prefetcher=self._enable_prefetchers,
        )

        # Create L1 data cache
        cluster.dcache = PrivateL1MOESICacheOptimised(
            size=self._l1d_size,
            assoc=self._l1d_assoc,
            network=self.ruby_system.network,
            core=core,
            cache_line_size=board.get_cache_line_size(),
            target_isa=board.get_processor().get_isa(),
            clk_domain=board.get_clock_domain(),
            is_icache=False,
            enable_prefetcher=self._enable_prefetchers,
        )

        # Create sequencers
        cluster.icache.sequencer = RubySequencer(
            version=core_num * 2,  # Even numbers for I-cache
            dcache=NULL,
            clk_domain=cluster.icache.clk_domain,
            ruby_system=self.ruby_system,
        )

        cluster.dcache.sequencer = RubySequencer(
            version=core_num * 2 + 1,  # Odd numbers for D-cache
            dcache=cluster.dcache.cache,
            clk_domain=cluster.dcache.clk_domain,
            ruby_system=self.ruby_system,
        )

        # Create private L2 cache if enabled

        cluster.l2cache = PrivateL2MOESICache(
            size=self._l2_size,
            assoc=self._l2_assoc,
            network=self.ruby_system.network,
            cache_line_size=board.get_cache_line_size(),
            clk_domain=board.get_clock_domain(),
            enable_prefetcher=self._enable_prefetchers,
        )
        cluster.l2cache.ruby_system = self.ruby_system

        # L1s route to L2
        cluster.icache.downstream_destinations = [cluster.l2cache]
        cluster.dcache.downstream_destinations = [cluster.l2cache]

        # Store last-level cache reference
        cluster.last_level = cluster.l2cache

        # Connect core ports
        core.connect_icache(cluster.icache.sequencer.in_ports)
        core.connect_dcache(cluster.dcache.sequencer.in_ports)

        # Connect walker ports
        core.connect_walker_ports(
            cluster.dcache.sequencer.in_ports,
            cluster.icache.sequencer.in_ports,
        )

        # Connect IO ports if available
        if board.has_io_bus():
            cluster.dcache.sequencer.connectIOPorts(board.get_io_bus())

        # Connect interrupt ports
        if board.get_processor().get_isa() == ISA.X86:
            int_req_port = cluster.dcache.sequencer.interrupt_out_port
            int_resp_port = cluster.dcache.sequencer.in_ports
            core.connect_interrupt(int_req_port, int_resp_port)
        else:
            core.connect_interrupt()

        # Set ruby system references
        cluster.icache.ruby_system = self.ruby_system
        cluster.dcache.ruby_system = self.ruby_system

        return cluster

    def _create_l3_banks(
        self, board: AbstractBoard
    ) -> List[SharedL3MOESICache]:
        """Create distributed L3 cache banks (HNF nodes)"""
        num_cores = len(board.get_processor().get_cores())
        num_banks = self._num_l3_banks or num_cores

        l3_banks = []
        for i in range(num_banks):
            l3_bank = SharedL3MOESICache(
                size=self._l3_size,
                assoc=self._l3_assoc,
                network=self.ruby_system.network,
                cache_line_size=board.get_cache_line_size(),
                clk_domain=board.get_clock_domain(),
                bank_id=i,
                num_banks=num_banks,
                memory_ranges=board.get_memory().get_addr_ranges(),
                enable_prefetcher=self._enable_prefetchers,
            )
            l3_bank.ruby_system = self.ruby_system
            l3_banks.append(l3_bank)

        return l3_banks

    def _create_memory_controllers(
        self, board: AbstractBoard
    ) -> List[MemoryController]:
        """Create memory controllers (SNF nodes)"""
        memory_controllers = []
        for rng, port in board.get_mem_ports():
            mc = MemoryController(self.ruby_system.network, [rng], port)
            mc.ruby_system = self.ruby_system
            memory_controllers.append(mc)
        return memory_controllers

    def _create_dma_controllers(
        self, board: AbstractBoard
    ) -> List[DMARequestor]:
        """Create DMA controllers (RNI nodes)"""
        dma_controllers = []
        num_cores = len(board.get_processor().get_cores())

        for i, port in enumerate(board.get_dma_ports()):
            ctrl = DMARequestor(
                self.ruby_system.network,
                board.get_cache_line_size(),
                board.get_clock_domain(),
            )
            # Assign version numbers after core sequencers
            version = num_cores * 2 + i
            ctrl.sequencer = RubySequencer(
                version=version,
                in_ports=port,
                ruby_system=self.ruby_system,
            )
            ctrl.sequencer.dcache = NULL
            ctrl.ruby_system = self.ruby_system
            ctrl.sequencer.ruby_system = self.ruby_system

            dma_controllers.append(ctrl)

        return dma_controllers

    def _setup_routing(self) -> None:
        """Configure downstream routing between cache levels"""
        # Route from last level caches to L3 banks
        for cluster in self.core_clusters:
            cluster.last_level.downstream_destinations = self.l3_banks

        for l3_bank in self.l3_banks:
            # Route L3 banks to directory
            l3_bank.downstream_destinations = self.directory

        # Route DMA controllers to L3 banks
        for dma_ctrl in self.dma_controllers:
            dma_ctrl.downstream_destinations = self.directory

        # Directory route to memory controllers
        self.directory.downstream_destinations = self.memory_controllers

    def _connect_controllers(self) -> None:
        """Connect all controllers to the network"""
        all_controllers = []

        # Add core cluster controllers
        for cluster in self.core_clusters:
            all_controllers.extend([cluster.icache, cluster.dcache])
            all_controllers.append(cluster.l2cache)

        # Add L3 banks
        all_controllers.extend(self.l3_banks)

        # Add directories (HNF nodes)
        all_controllers.extend(self.directory)

        # Add memory controllers
        all_controllers.extend(self.memory_controllers)

        # Add DMA controllers
        all_controllers.extend(self.dma_controllers)

        # Connect to network
        self.ruby_system.network.connectControllers(all_controllers)
        self.ruby_system.network.setup_buffers()

    @overrides(AbstractRubyCacheHierarchy)
    def _reset_version_numbers(self):
        """Reset version counters for reproducible builds"""
        from .nodes.abstract_node import AbstractNode

        AbstractNode._version = 0
        MemoryController._version = 0
