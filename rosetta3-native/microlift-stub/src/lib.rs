//! A stand-in for Microlift, so that rosetta3 builds without it.
//!
//! It has the types and functions rosetta3 names, with the shapes its call sites
//! need. What a native run can reach is implemented: the register context, the
//! per-thread link tables, the pinned-register configuration, the SysV argument
//! placement and x86-64 disassembly. What only emulation needs - lifting,
//! compiling and entering x86-64 code - reports [`EMULATION_UNAVAILABLE`]: as an
//! error where the signature has one, and as a panic or an abort where it has not.
//! A rosetta3 built with this crate runs guests natively only.

pub use cranelift_codegen;
pub use cranelift_frontend;
pub use cranelift_native;
pub use function_traits;
pub use target_lexicon;

pub const EMULATION_UNAVAILABLE: &str =
    "this rosetta3 was built without Microlift, so it cannot emulate x86-64; only native execution works";

#[track_caller]
pub(crate) fn unavailable() -> ! {
    panic!("{EMULATION_UNAVAILABLE}")
}

#[derive(Debug, Clone)]
pub enum MicroliftError {
    CraneliftError(String),
    Unavailable,
}

impl std::fmt::Display for MicroliftError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            MicroliftError::CraneliftError(e) => write!(f, "cranelift: {e}"),
            MicroliftError::Unavailable => f.write_str(EMULATION_UNAVAILABLE),
        }
    }
}

impl std::error::Error for MicroliftError {}

pub type Result<T> = std::result::Result<T, MicroliftError>;

pub mod os {
    /// The calling convention host functions use.
    pub fn get_host_calling_conv() -> cranelift_codegen::isa::CallConv {
        cranelift_codegen::isa::CallConv::triple_default(&target_lexicon::Triple::host())
    }
}

pub mod arch {
    pub mod x86_64 {
        /// The x86-64 architecture marker the lifter is generic over.
        #[derive(Debug, Clone, Copy, Default)]
        pub struct X86_64;

        pub mod context {
            /// Host functions the lifted code calls out through.
            #[repr(C)]
            #[derive(Debug, Clone, Copy)]
            pub struct X86_64Callbacks {
                pub handle_syscall: unsafe extern "C" fn(*mut RegisterContext),
                pub interpreter_fallback: unsafe extern "C" fn(*mut RegisterContext, u64),
            }

            unsafe extern "C" fn no_syscall(_ctx: *mut RegisterContext) {
                eprintln!("{}", crate::EMULATION_UNAVAILABLE);
                std::process::abort()
            }

            unsafe extern "C" fn no_interpreter(_ctx: *mut RegisterContext, _count: u64) {
                eprintln!("{}", crate::EMULATION_UNAVAILABLE);
                std::process::abort()
            }

            impl Default for X86_64Callbacks {
                fn default() -> Self {
                    X86_64Callbacks {
                        handle_syscall: no_syscall,
                        interpreter_fallback: no_interpreter,
                    }
                }
            }

            /// The guest's x86-64 register file.
            #[repr(C)]
            #[derive(Debug, Clone, Copy)]
            pub struct RegisterContext {
                pub rax: u64,
                pub rcx: u64,
                pub rdx: u64,
                pub rbx: u64,
                pub rsp: u64,
                pub rbp: u64,
                pub rsi: u64,
                pub rdi: u64,
                pub r8: u64,
                pub r9: u64,
                pub r10: u64,
                pub r11: u64,
                pub r12: u64,
                pub r13: u64,
                pub r14: u64,
                pub r15: u64,
                pub rip: u64,
                pub xmm: [[u64; 2]; 16],
                pub cf: u8,
                pub pf: u8,
                pub af: u8,
                pub zf: u8,
                pub sf: u8,
                pub of: u8,
                pub df: u8,
                pub fs_base: u64,
                pub gs_base: u64,
                pub exit_reason: u32,
                pub exit_code: u32,
                pub imported_function_va: u64,
                pub link_table: *mut crate::core::link::BlockLinkEntry,
                pub callbacks: X86_64Callbacks,
            }

            impl RegisterContext {
                pub fn new() -> Self {
                    RegisterContext {
                        rax: 0,
                        rcx: 0,
                        rdx: 0,
                        rbx: 0,
                        rsp: 0,
                        rbp: 0,
                        rsi: 0,
                        rdi: 0,
                        r8: 0,
                        r9: 0,
                        r10: 0,
                        r11: 0,
                        r12: 0,
                        r13: 0,
                        r14: 0,
                        r15: 0,
                        rip: 0,
                        xmm: [[0; 2]; 16],
                        cf: 0,
                        pf: 0,
                        af: 0,
                        zf: 0,
                        sf: 0,
                        of: 0,
                        df: 0,
                        fs_base: 0,
                        gs_base: 0,
                        exit_reason: 0,
                        exit_code: 0,
                        imported_function_va: 0,
                        link_table: std::ptr::null_mut(),
                        callbacks: X86_64Callbacks::default(),
                    }
                }
            }

            impl Default for RegisterContext {
                fn default() -> Self {
                    Self::new()
                }
            }

            // The link-table pointer is an address lifted code reads; nothing in this
            // crate dereferences it.
            unsafe impl Send for RegisterContext {}
            unsafe impl Sync for RegisterContext {}
        }

        pub mod abi {
            /// The x86-64 register a guest ABI register names.
            pub fn x86_reg(reg: crate::core::abi::GuestReg) -> Result<iced_x86::Register, ()> {
                Ok(reg.0)
            }
        }

        pub mod decode {
            /// Decode `bytes` as x86-64 code loaded at `ip`.
            pub fn decode_region(bytes: &[u8], ip: u64) -> Vec<iced_x86::Instruction> {
                let mut decoder = iced_x86::Decoder::with_ip(64, bytes, ip, iced_x86::DecoderOptions::NONE);
                let mut out = Vec::new();
                while decoder.can_decode() {
                    out.push(decoder.decode());
                }
                out
            }
        }

        pub mod pin {
            use crate::core::pin::PinnedReg;

            pub const RAX: PinnedReg = PinnedReg::gpr(0);
            pub const RCX: PinnedReg = PinnedReg::gpr(1);
            pub const RDX: PinnedReg = PinnedReg::gpr(2);
            pub const RBX: PinnedReg = PinnedReg::gpr(3);
            pub const RSP: PinnedReg = PinnedReg::gpr(4);
            pub const RBP: PinnedReg = PinnedReg::gpr(5);
            pub const RSI: PinnedReg = PinnedReg::gpr(6);
            pub const RDI: PinnedReg = PinnedReg::gpr(7);
            pub const R8: PinnedReg = PinnedReg::gpr(8);
            pub const R9: PinnedReg = PinnedReg::gpr(9);
            pub const R10: PinnedReg = PinnedReg::gpr(10);
            pub const R11: PinnedReg = PinnedReg::gpr(11);
            pub const R12: PinnedReg = PinnedReg::gpr(12);
            pub const R13: PinnedReg = PinnedReg::gpr(13);
            pub const R14: PinnedReg = PinnedReg::gpr(14);
            pub const R15: PinnedReg = PinnedReg::gpr(15);

            pub const fn xmm(n: u16) -> PinnedReg {
                PinnedReg::vector(n)
            }
        }
    }
}

pub mod core {
    use std::ops::Range;

    pub mod exit {
        #[repr(u32)]
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
        pub enum ExitReason {
            None = 0,
            UnknownJump = 1,
            ImportedFunctionCall = 2,
            IndirectCall = 3,
            Breakpoint = 4,
            Cpuid = 5,
        }

        impl std::fmt::Display for ExitReason {
            fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                std::fmt::Debug::fmt(self, f)
            }
        }

        impl TryFrom<u32> for ExitReason {
            type Error = ();
            fn try_from(value: u32) -> Result<Self, ()> {
                Ok(match value {
                    0 => ExitReason::None,
                    1 => ExitReason::UnknownJump,
                    2 => ExitReason::ImportedFunctionCall,
                    3 => ExitReason::IndirectCall,
                    4 => ExitReason::Breakpoint,
                    5 => ExitReason::Cpuid,
                    _ => return Err(()),
                })
            }
        }

        #[repr(u32)]
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
        pub enum TransferKind {
            DirectJump = 0,
            ConditionalJump = 1,
            IndirectJump = 2,
            DirectCall = 3,
            Return = 4,
            RegionTail = 5,
            NoResumePoint = 6,
        }

        impl TryFrom<u32> for TransferKind {
            type Error = ();
            fn try_from(value: u32) -> Result<Self, ()> {
                Ok(match value {
                    0 => TransferKind::DirectJump,
                    1 => TransferKind::ConditionalJump,
                    2 => TransferKind::IndirectJump,
                    3 => TransferKind::DirectCall,
                    4 => TransferKind::Return,
                    5 => TransferKind::RegionTail,
                    6 => TransferKind::NoResumePoint,
                    _ => return Err(()),
                })
            }
        }

        #[derive(Debug, Clone)]
        pub struct MemorySegment {
            pub base: u64,
            pub bytes: Vec<u8>,
            pub writable: bool,
        }

        impl MemorySegment {
            pub fn new(base: u64, bytes: Vec<u8>, writable: bool) -> Self {
                MemorySegment { base, bytes, writable }
            }
        }
    }

    pub mod optimize {
        #[derive(Debug, Clone, Copy, Default)]
        pub struct Optimizations;

        impl Optimizations {
            pub fn all() -> Self {
                Optimizations
            }
        }
    }

    pub mod specialize {
        #[derive(Debug, Clone, Copy, Default)]
        pub struct Specialization;

        impl Specialization {
            pub fn host() -> Self {
                Specialization
            }
            pub fn generic() -> Self {
                Specialization
            }
        }
    }

    pub mod pin {
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
        pub enum RegClass {
            General,
            Vector,
        }

        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
        pub struct PinnedReg {
            pub class: RegClass,
            pub index: u16,
        }

        impl PinnedReg {
            pub const fn gpr(index: u16) -> Self {
                PinnedReg { class: RegClass::General, index }
            }
            pub const fn vector(index: u16) -> Self {
                PinnedReg { class: RegClass::Vector, index }
            }
            const fn bit(self) -> u32 {
                match self.class {
                    RegClass::General => 1 << self.index,
                    RegClass::Vector => 1 << (16 + self.index),
                }
            }
        }

        /// A set of guest registers kept in host registers across links.
        #[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
        pub struct PinnedAbi {
            bits: u32,
        }

        impl PinnedAbi {
            pub fn new() -> Self {
                PinnedAbi { bits: 0 }
            }
            pub fn with(self, reg: PinnedReg) -> Self {
                PinnedAbi { bits: self.bits | reg.bit() }
            }
            pub fn contains(&self, reg: PinnedReg) -> bool {
                self.bits & reg.bit() != 0
            }
            pub fn count(&self, class: RegClass) -> usize {
                let mask = match class {
                    RegClass::General => 0x0000_ffff,
                    RegClass::Vector => 0xffff_0000,
                };
                (self.bits & mask).count_ones() as usize
            }
            pub fn is_empty(&self) -> bool {
                self.bits == 0
            }
            pub fn len(&self) -> usize {
                self.bits.count_ones() as usize
            }
            /// Nothing is pinned by this crate, so no budget can be exceeded.
            pub fn check_host_budget(&self) -> Result<(), String> {
                Ok(())
            }
        }

        #[derive(Debug, Clone, Copy)]
        pub struct RegisterBudget {
            pub gpr: u32,
            pub vector: u32,
        }

        impl RegisterBudget {
            pub fn for_arch(_arch: target_lexicon::Architecture) -> Option<Self> {
                None
            }
        }
    }

    pub mod callret {
        #[derive(Debug, Clone, Copy)]
        pub struct CallRetConfig {
            pub frames: u32,
        }

        impl CallRetConfig {
            pub fn new(frames: u32) -> Self {
                CallRetConfig { frames }
            }
        }
    }

    pub mod hostframe {
        #[derive(Debug, Clone, Copy)]
        pub struct HostFrameConfig {
            pub depth: u32,
        }

        impl HostFrameConfig {
            pub fn new(depth: u32) -> Self {
                HostFrameConfig { depth }
            }
        }
    }

    pub mod link {
        /// One row of a thread's block-link table.
        #[repr(C)]
        #[derive(Debug, Clone, Copy, Default)]
        pub struct BlockLinkEntry {
            pub guest: u64,
            pub host: u64,
        }

        /// The geometry of a thread's block-link table.
        #[derive(Debug, Clone, Copy)]
        pub struct BlockLinkConfig {
            pub entries: u32,
            pub pinned: Option<super::pin::PinnedAbi>,
        }

        impl BlockLinkConfig {
            pub fn new(entries: u32) -> Self {
                BlockLinkConfig { entries: entries.max(1), pinned: None }
            }
            pub fn pinned(self, abi: super::pin::PinnedAbi) -> Self {
                BlockLinkConfig { pinned: Some(abi), ..self }
            }
            /// The row a guest address maps to.
            pub fn slot(&self, pc: u64) -> usize {
                (pc % u64::from(self.entries)) as usize
            }
        }

        #[derive(Debug, Clone, Copy)]
        pub struct LinkSite {
            pub guest: u64,
            pub cell: u32,
        }
    }

    pub mod srcloc {
        /// Host code offsets `start..end` that came from the guest instruction at
        /// `guest_va`.
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub struct GuestCodeRange {
            pub start: u32,
            pub end: u32,
            pub guest_va: u64,
        }

        impl GuestCodeRange {
            pub fn contains(&self, offset: u32) -> bool {
                self.start <= offset && offset < self.end
            }
        }

        #[derive(Debug, Clone, Default)]
        pub struct GuestCodeMap {
            ranges: Vec<GuestCodeRange>,
        }

        impl GuestCodeMap {
            pub fn from_ranges(mut ranges: Vec<GuestCodeRange>) -> Self {
                ranges.sort_by_key(|r| r.start);
                GuestCodeMap { ranges }
            }
            pub fn ranges(&self) -> &[GuestCodeRange] {
                &self.ranges
            }
            pub fn is_empty(&self) -> bool {
                self.ranges.is_empty()
            }
        }
    }

    pub mod abi {
        use function_traits::FunctionArgumentType as ArgType;

        /// A guest register, by its iced-x86 name.
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub struct GuestReg(pub iced_x86::Register);

        /// Where the guest ABI puts one argument.
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub enum ArgLocation {
            IntReg(GuestReg),
            FpReg(u8),
            /// A byte offset from the stack pointer at the call.
            Stack(u32),
        }

        /// A guest calling convention.
        #[derive(Debug, Clone, PartialEq, Eq)]
        pub struct CallAbi {
            pub int_args: Vec<iced_x86::Register>,
            pub fp_args: u8,
        }

        fn is_fp(ty: ArgType) -> bool {
            matches!(ty, ArgType::SinglePrecisionFp | ArgType::DoublePrecisionFp)
        }

        impl CallAbi {
            /// System V AMD64: six integer registers, eight vector registers, then
            /// the stack in eight-byte slots.
            pub fn sysv_amd64() -> Self {
                use iced_x86::Register::*;
                CallAbi { int_args: vec![RDI, RSI, RDX, RCX, R8, R9], fp_args: 8 }
            }

            /// Where each argument of `types` goes, in order. `None` for a type the
            /// convention cannot pass.
            pub fn place_args(&self, types: &[ArgType]) -> Option<Vec<(ArgType, ArgLocation)>> {
                let (mut ints, mut fps, mut stack) = (0usize, 0u8, 0u32);
                let mut placed = Vec::with_capacity(types.len());
                for &ty in types {
                    if matches!(ty, ArgType::None) {
                        return None;
                    }
                    let location = if is_fp(ty) && fps < self.fp_args {
                        fps += 1;
                        ArgLocation::FpReg(fps - 1)
                    } else if !is_fp(ty) && ints < self.int_args.len() {
                        ints += 1;
                        ArgLocation::IntReg(GuestReg(self.int_args[ints - 1]))
                    } else {
                        stack += 8;
                        ArgLocation::Stack(stack - 8)
                    };
                    placed.push((ty, location));
                }
                Some(placed)
            }

            /// Whether a lifted call could pass `types` straight through.
            pub fn can_fast_call(&self, types: &[ArgType]) -> bool {
                self.place_args(types)
                    .is_some_and(|placed| placed.iter().all(|(_, l)| !matches!(l, ArgLocation::Stack(_))))
            }
        }
    }

    pub mod bridge {
        /// What a bridge function returns to continue through the block links.
        pub const RESUME_LINKED: u32 = 2;

        #[derive(Debug, Clone)]
        pub struct BridgeImport {
            pub name: &'static str,
            pub entry: *const u8,
        }

        // The entry is the address of a function, the same on every thread.
        unsafe impl Send for BridgeImport {}
        unsafe impl Sync for BridgeImport {}

        impl BridgeImport {
            pub fn direct(name: &'static str, entry: *const u8) -> Self {
                BridgeImport { name, entry }
            }
        }

        #[derive(Debug, Clone)]
        pub enum ImportKind {
            Fast(super::FastImport),
            Slow,
            Bridge(BridgeImport),
        }

        /// Imports that were not known when a `LifterConfig` was built.
        pub trait ImportRegistry: Send + Sync + std::fmt::Debug {
            fn lookup(&self, va: u64) -> Option<ImportKind>;
        }
    }

    #[derive(Debug, Clone)]
    pub struct FastImport {
        pub symbol: function_traits::DynamicSymbol,
        pub abi: abi::CallAbi,
    }

    #[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
    pub enum AddressMode {
        #[default]
        Direct,
    }

    #[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
    pub enum Atomic128Mode {
        #[default]
        Interpret,
        Native,
        Emulated,
    }

    #[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
    pub enum VmResumptionPoints {
        #[default]
        ExitOnly,
    }

    #[derive(Debug, Clone, Default)]
    pub struct LifterConfig {
        pub interpreter_fallback: bool,
        pub use_vector_xmm: bool,
        pub outline_helpers: bool,
        pub address_mode: AddressMode,
        pub atomic128: Atomic128Mode,
        pub specialization: specialize::Specialization,
        pub eliminate_dead_flags: bool,
        pub resumption: VmResumptionPoints,
        pub strict_resume: bool,
        pub block_link: Option<link::BlockLinkConfig>,
        pub inline_link_probes: bool,
        pub link_cells: bool,
        pub call_return_stack: Option<callret::CallRetConfig>,
        pub host_call_frames: Option<hostframe::HostFrameConfig>,
        pub optimizations: optimize::Optimizations,
        pub exit_trampoline_address: Option<u64>,
        pub analysis_mode: bool,
        pub resume_points: Vec<u64>,
        pub block_ranges: Vec<Range<u64>>,
        pub segments: std::sync::Arc<Vec<exit::MemorySegment>>,
        pub imports: std::sync::Arc<std::collections::HashMap<u64, bridge::ImportKind>>,
        pub import_registry: Option<std::sync::Arc<dyn bridge::ImportRegistry>>,
    }

    impl LifterConfig {
        pub fn set_segments(&mut self, segments: Vec<exit::MemorySegment>) {
            self.segments = std::sync::Arc::new(segments);
        }
        pub fn register_fast_import(
            &mut self,
            va: u64,
            symbol: function_traits::DynamicSymbol,
            abi: abi::CallAbi,
        ) {
            std::sync::Arc::make_mut(&mut self.imports)
                .insert(va, bridge::ImportKind::Fast(FastImport { symbol, abi }));
        }
        pub fn register_slow_import(&mut self, va: u64) {
            std::sync::Arc::make_mut(&mut self.imports).insert(va, bridge::ImportKind::Slow);
        }
        pub fn register_bridge_import(&mut self, va: u64, import: bridge::BridgeImport) {
            std::sync::Arc::make_mut(&mut self.imports).insert(va, bridge::ImportKind::Bridge(import));
        }
        pub fn set_import_registry(&mut self, registry: std::sync::Arc<dyn bridge::ImportRegistry>) {
            self.import_registry = Some(registry);
        }
    }

    /// What lifting a region produced.
    #[derive(Debug, Clone, Default)]
    pub struct LiftedCode {
        pub resume_points: Vec<u64>,
        pub covered: Vec<Range<u64>>,
        pub data_dependencies: Vec<Range<u64>>,
        pub link_sites: Vec<link::LinkSite>,
    }

    pub fn lift_to_function<A>(
        _bytes: &[u8],
        _start: u64,
        _config: LifterConfig,
    ) -> crate::Result<cranelift_codegen::ir::Function> {
        Err(crate::MicroliftError::Unavailable)
    }

    pub fn lift_to_code<A>(_bytes: &[u8], _start: u64, _config: LifterConfig) -> crate::Result<LiftedCode> {
        Err(crate::MicroliftError::Unavailable)
    }

    pub fn lift_to_function_with_call_targets<A>(
        _bytes: &[u8],
        _start: u64,
        _config: LifterConfig,
    ) -> crate::Result<(cranelift_codegen::ir::Function, Vec<u64>)> {
        Err(crate::MicroliftError::Unavailable)
    }
}

pub mod backend {
    /// The two entry points of a compiled region.
    #[derive(Debug)]
    pub struct CompiledRegion {
        pub entry: *const u8,
        pub link_target: *const u8,
    }

    unsafe impl Send for CompiledRegion {}
    unsafe impl Sync for CompiledRegion {}

    /// The JIT. Never constructed by this crate.
    #[derive(Debug)]
    pub struct CraneliftBackend {
        _never: std::convert::Infallible,
    }

    impl CraneliftBackend {
        pub fn with_flags(_flags: cranelift_codegen::settings::Flags, _isa: &[(&str, &str)]) -> crate::Result<Self> {
            Err(crate::MicroliftError::Unavailable)
        }
        pub fn compile(&mut self, _function: cranelift_codegen::ir::Function) -> crate::Result<*const u8> {
            match self._never {}
        }
        pub fn compile_lifted_region(
            &mut self,
            _lifted: crate::core::LiftedCode,
        ) -> crate::Result<(CompiledRegion, crate::core::srcloc::GuestCodeMap)> {
            match self._never {}
        }
    }

    pub struct ArtifactBackend;
}

pub mod runtime {
    use std::sync::Arc;

    use crate::arch::x86_64::context::RegisterContext;
    use crate::core::callret::CallRetConfig;
    use crate::core::exit::ExitReason;
    use crate::core::link::{BlockLinkConfig, BlockLinkEntry, LinkSite};

    #[derive(Debug, Clone, Copy, Default)]
    pub struct CacheStats {
        pub lookups: u64,
        pub hits: u64,
        pub cells_installed: u64,
        pub cells_retracted: u64,
        pub links_followed: u64,
    }

    impl CacheStats {
        /// The share of lookups that missed, or `None` before the first lookup.
        pub fn miss_rate(&self) -> Option<f64> {
            (self.lookups > 0).then(|| (self.lookups - self.hits) as f64 / self.lookups as f64)
        }
    }

    /// Compiled guest code. Never constructed by this crate, since nothing is
    /// compiled.
    #[derive(Debug)]
    pub struct Translation {
        pub start: u64,
        pub resume_points: Vec<u64>,
        pub covered: Vec<std::ops::Range<u64>>,
        pub data_dependencies: Vec<std::ops::Range<u64>>,
        pub link_sites: Vec<LinkSite>,
        pub region: crate::backend::CompiledRegion,
        _backend: crate::backend::CraneliftBackend,
    }

    impl Translation {
        pub fn from_compiled(
            region: crate::backend::CompiledRegion,
            start: u64,
            resume_points: Vec<u64>,
            covered: Vec<std::ops::Range<u64>>,
            data_dependencies: Vec<std::ops::Range<u64>>,
            link_sites: Vec<LinkSite>,
            backend: crate::backend::CraneliftBackend,
        ) -> Self {
            Translation {
                start,
                resume_points,
                covered,
                data_dependencies,
                link_sites,
                region,
                _backend: backend,
            }
        }
        pub fn covers(&self, pc: u64) -> bool {
            self.resume_points.contains(&pc)
        }
    }

    /// The shared translation cache. Always empty here.
    #[derive(Debug, Default)]
    pub struct CodeCache {
        stats: CacheStats,
    }

    impl CodeCache {
        pub fn new() -> Self {
            CodeCache::default()
        }
        pub fn len(&self) -> usize {
            0
        }
        pub fn is_empty(&self) -> bool {
            true
        }
        pub fn stats(&self) -> CacheStats {
            self.stats
        }
        pub fn note_links_followed(&self, _n: usize) {}
        pub fn cell_counts(&self) -> (usize, usize) {
            (0, 0)
        }
        pub fn insert(&mut self, translation: Translation) -> Arc<Translation> {
            Arc::new(translation)
        }
        pub fn lookup(&self, _pc: u64) -> Option<Arc<Translation>> {
            None
        }
        pub fn reclaim(&mut self) {}
    }

    /// Rows of a thread's link table that name a translation.
    #[derive(Debug, Default)]
    pub struct LinkTable;

    impl LinkTable {
        pub fn get(&self, _pc: u64) -> Option<BlockLinkEntry> {
            None
        }
    }

    /// A thread's block-link table and call-return ring.
    #[derive(Debug)]
    pub struct ThreadLinks {
        config: BlockLinkConfig,
        rows: Box<[std::cell::UnsafeCell<BlockLinkEntry>]>,
        table: LinkTable,
        _call_return: Option<CallRetConfig>,
    }

    impl ThreadLinks {
        pub fn new(config: BlockLinkConfig, call_return: Option<CallRetConfig>) -> Self {
            let rows = (0..config.entries)
                .map(|_| std::cell::UnsafeCell::new(BlockLinkEntry::default()))
                .collect();
            ThreadLinks { config, rows, table: LinkTable, _call_return: call_return }
        }
        pub fn config(&self) -> BlockLinkConfig {
            self.config
        }
        /// The table's address, as `RegisterContext::link_table` holds it.
        pub fn link_table_ptr(&self) -> *mut BlockLinkEntry {
            self.rows.as_ptr() as *mut BlockLinkEntry
        }
        /// Point `ctx` at this thread's tables.
        pub fn attach(&self, ctx: &mut RegisterContext) {
            ctx.link_table = self.link_table_ptr();
        }
        pub fn table(&self) -> &LinkTable {
            &self.table
        }
        /// Nothing is linked, because nothing is compiled.
        pub fn publish_at(&mut self, _translation: &Arc<Translation>, _pc: u64) -> bool {
            false
        }
        pub fn publish(&mut self, _translation: &Arc<Translation>) {}
        /// Continue an outer entry's call-return ring. There is no ring here.
        pub fn resume_call_return(_ctx: &mut RegisterContext, _outer: Option<&RegisterContext>) {}
    }

    /// How an entry into compiled code ended.
    #[derive(Debug, Clone, Copy)]
    pub struct Entered {
        pub exit: ExitReason,
        pub links: usize,
    }

    /// Run compiled code. Unreachable here, since nothing is ever compiled.
    ///
    /// # Safety
    ///
    /// As Microlift's: `translation` was compiled for this context's layout.
    pub unsafe fn enter(_translation: &Translation, _ctx: &mut RegisterContext) -> Entered {
        crate::unavailable()
    }

    /// [`enter`], calling `on_link` for every linked hop.
    ///
    /// # Safety
    ///
    /// As [`enter`].
    pub unsafe fn enter_with<F>(_translation: &Translation, _ctx: &mut RegisterContext, _on_link: F) -> Entered
    where
        F: FnMut(ExitReason, &mut RegisterContext),
    {
        crate::unavailable()
    }
}
