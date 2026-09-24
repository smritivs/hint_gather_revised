//===- HintGatherPass.cpp - HINT.GATHER detection / profiling / emission --===//
//
// Role: out-of-tree LLVM pass *plugin* (new pass manager) implementing the
// LLVM node of the HINT.GATHER CHIA project.
//
// Normative spec: docs/DESIGN.md -- section 1.1 (encoding), section 3 (genome
// JSON), section 4.1 (this pass's contract: options, modes, detection rule,
// hint_sites.json schema, pc_symbol).  Anything that crosses a component
// boundary is defined there, not here.
//
// NOTE (deliverable folding): the profile-mode instrumentation that was
// sketched as a separate `StrideEntropyPass.cpp` lives in this file
// (`instrumentSite`).  Splitting it would have meant duplicating the entire
// detection pipeline, because ranking in `emit` mode consumes exactly the
// site ids that `profile` mode assigns.  See README.md.
//
// Modes (-hg-mode=):
//   analyze : detect, write hint_sites.json, change no IR.
//   profile : detect, insert __hg_profile_access(site_id, addr) calls, write
//             hint_sites.json.
//   emit    : detect, insert the `.insn r 0x0b, ...` hint + a __hg_site_<id>
//             label before the demand load, write hint_sites.json.
//
// Robustness policy: this pass runs inside an autonomous repair loop.  It must
// never crash and never produce invalid IR.  Every dyn_cast is guarded and any
// unsupported shape is recorded as a `skip_reason` string on the site instead
// of being asserted on.
//
//===----------------------------------------------------------------------===//

#include "llvm/ADT/APInt.h"
#include "llvm/ADT/DenseMap.h"
#include "llvm/ADT/SmallVector.h"
#include "llvm/ADT/StringRef.h"
#include "llvm/ADT/Twine.h"
#include "llvm/Analysis/LoopInfo.h"
#include "llvm/Analysis/ScalarEvolution.h"
#include "llvm/Analysis/ScalarEvolutionExpressions.h"
#include "llvm/Analysis/ValueTracking.h"
#include "llvm/Config/llvm-config.h"
#include "llvm/IR/BasicBlock.h"
#include "llvm/IR/Constants.h"
#include "llvm/IR/DataLayout.h"
#include "llvm/IR/DebugInfoMetadata.h"
#include "llvm/IR/DebugLoc.h"
#include "llvm/IR/DerivedTypes.h"
#include "llvm/IR/Function.h"
#include "llvm/IR/IRBuilder.h"
#include "llvm/IR/InlineAsm.h"
#include "llvm/IR/Instructions.h"
#include "llvm/IR/Module.h"
#include "llvm/IR/PassManager.h"
#include "llvm/IR/Type.h"
#include "llvm/Passes/PassBuilder.h"
#include "llvm/Passes/PassPlugin.h"
#include "llvm/Support/Compiler.h"
#include "llvm/Support/CommandLine.h"
#include "llvm/Support/Error.h"
#include "llvm/Support/ErrorOr.h"
#include "llvm/Support/FileSystem.h"
#include "llvm/Support/JSON.h"
#include "llvm/Support/MemoryBuffer.h"
#include "llvm/Support/raw_ostream.h"

#include <algorithm>
#include <cstdint>
#include <cstdio>
#include <memory>
#include <string>
#include <system_error>
#include <vector>

using namespace llvm;

//===----------------------------------------------------------------------===//
// Command line options (DESIGN.md section 4.1)
//===----------------------------------------------------------------------===//

static cl::opt<std::string>
    HGGenome("hg-genome", cl::desc("Path to the genome JSON (DESIGN.md s3)"),
             cl::value_desc("path"), cl::init(""));

static cl::opt<std::string>
    HGReport("hg-report",
             cl::desc("Path of the hint_sites.json report to write"),
             cl::value_desc("path"), cl::init("hint_sites.json"));

static cl::opt<std::string>
    HGProfile("hg-profile",
              cl::desc("Path to hg_profile.json produced by a previous "
                       "-hg-mode=profile run; used to rank candidates"),
              cl::value_desc("path"), cl::init(""));

static cl::opt<std::string>
    HGMode("hg-mode", cl::desc("One of: analyze, profile, emit"),
           cl::value_desc("mode"), cl::init("analyze"));

static cl::opt<bool> HGVerbose("hg-verbose",
                               cl::desc("Chatty diagnostics on stderr"),
                               cl::init(false));

static cl::opt<bool> HGEmitSiteLabels(
    "hg-emit-site-labels",
    cl::desc("Emit __hg_site_<id> labels in analyze mode for PC resolution"),
    cl::init(false));

namespace {

//===----------------------------------------------------------------------===//
// Modes
//===----------------------------------------------------------------------===//

enum class Mode { Analyze, Profile, Emit };

Mode parseMode(StringRef S) {
  if (S == "emit")
    return Mode::Emit;
  if (S == "profile")
    return Mode::Profile;
  // Unknown strings fall back to the read-only mode: a typo on the driver
  // command line must never silently mutate the program.
  if (S != "analyze" && !S.empty())
    errs() << "[hint-gather] warning: unknown -hg-mode='" << S
           << "', falling back to 'analyze'\n";
  return Mode::Analyze;
}

//===----------------------------------------------------------------------===//
// Genome (DESIGN.md section 3).  Only the keys marked "consumed by LLVM" are
// read here; unknown keys are ignored.  Missing keys take the documented
// default shown in the section 3 example.
//===----------------------------------------------------------------------===//

struct Genome {
  int64_t HintDistance = 32;         // 1..512
  int64_t Fanout = 1;                // 1..8 (instruction encodes fanout-1)
  double EntropyThreshold = 0.35;    // 0.0..1.0
  std::string Variant = "chase";     // "value" | "chase"
  std::string PrefetchLevel = "L1D"; // "L1D" | "L2C"
  bool Droppable = true;
  int64_t MaxHintsPerLoop = 2; // 1..8
  int64_t MinTripCount = 64;

  std::string Hash = "00000000"; // 8 hex chars, see hashGenomeText()

  bool isChase() const { return Variant != "value"; }
  unsigned variantBit() const { return isChase() ? 1u : 0u; }
  unsigned levelBit() const { return PrefetchLevel == "L2C" ? 1u : 0u; }
  unsigned dropBit() const { return Droppable ? 1u : 0u; }

  // funct3 = [0] variant | [1] level | [2] drop   (DESIGN.md section 1.1).
  unsigned funct3() const {
    return (variantBit() & 1u) | ((levelBit() & 1u) << 1) |
           ((dropBit() & 1u) << 2);
  }
  // funct7 = [1:0] shift | [4:2] fanout-1 | [6:5] reserved 0.
  unsigned funct7(unsigned Shift) const {
    unsigned F = static_cast<unsigned>(Fanout - 1) & 0x7u;
    return (Shift & 0x3u) | (F << 2);
  }
};

template <typename T> T clampVal(T V, T Lo, T Hi) {
  return V < Lo ? Lo : (V > Hi ? Hi : V);
}

/// FNV-1a over the genome text with all whitespace removed, so that
/// pretty-printing differences do not change the hash.  8 lowercase hex chars,
/// matching the `"genome_hash": "ab12cd34"` example in DESIGN.md section 4.1.
std::string hashGenomeText(StringRef Text) {
  uint32_t H = 2166136261u;
  for (char C : Text) {
    unsigned char U = static_cast<unsigned char>(C);
    if (U == ' ' || U == '\t' || U == '\n' || U == '\r' || U == '\f' ||
        U == '\v')
      continue;
    H ^= static_cast<uint32_t>(U);
    H *= 16777619u;
  }
  char Buf[16];
  std::snprintf(Buf, sizeof(Buf), "%08x", H);
  return std::string(Buf);
}

bool getInt(const json::Object &O, StringRef Key, int64_t &Out) {
  if (auto V = O.getInteger(Key)) {
    Out = *V;
    return true;
  }
  // Tolerate integers serialised as doubles ("fanout": 2.0).
  if (auto V = O.getNumber(Key)) {
    Out = static_cast<int64_t>(*V);
    return true;
  }
  return false;
}

void loadGenome(Genome &G) {
  if (HGGenome.empty()) {
    G.Hash = hashGenomeText("default");
    if (HGVerbose)
      errs() << "[hint-gather] no -hg-genome given, using DESIGN.md defaults\n";
    return;
  }

  ErrorOr<std::unique_ptr<MemoryBuffer>> BufOrErr =
      MemoryBuffer::getFile(HGGenome);
  if (!BufOrErr) {
    errs() << "[hint-gather] warning: cannot read genome '" << HGGenome
           << "': " << BufOrErr.getError().message()
           << " -- using DESIGN.md defaults\n";
    G.Hash = hashGenomeText("default");
    return;
  }
  StringRef Text = (*BufOrErr)->getBuffer();
  G.Hash = hashGenomeText(Text);

  Expected<json::Value> Parsed = json::parse(Text);
  if (!Parsed) {
    errs() << "[hint-gather] warning: genome '" << HGGenome
           << "' is not valid JSON (" << toString(Parsed.takeError())
           << ") -- using DESIGN.md defaults\n";
    return;
  }
  const json::Object *O = Parsed->getAsObject();
  if (!O) {
    errs() << "[hint-gather] warning: genome '" << HGGenome
           << "' is not a JSON object -- using DESIGN.md defaults\n";
    return;
  }

  int64_t I = 0;
  if (getInt(*O, "hint_distance", I))
    G.HintDistance = clampVal<int64_t>(I, 1, 512);
  if (getInt(*O, "fanout", I))
    G.Fanout = clampVal<int64_t>(I, 1, 8);
  if (getInt(*O, "max_hints_per_loop", I))
    G.MaxHintsPerLoop = clampVal<int64_t>(I, 1, 8);
  if (getInt(*O, "min_trip_count", I))
    G.MinTripCount = I < 0 ? 0 : I;
  if (auto D = O->getNumber("entropy_threshold"))
    G.EntropyThreshold = clampVal<double>(*D, 0.0, 1.0);
  if (auto S = O->getString("variant"))
    G.Variant = (*S == "value") ? "value" : "chase";
  if (auto S = O->getString("prefetch_level"))
    G.PrefetchLevel = (*S == "L2C") ? "L2C" : "L1D";
  if (auto B = O->getBoolean("droppable"))
    G.Droppable = *B;

  if (HGVerbose)
    errs() << "[hint-gather] genome " << G.Hash
           << ": distance=" << G.HintDistance << " fanout=" << G.Fanout
           << " variant=" << G.Variant << " level=" << G.PrefetchLevel
           << " droppable=" << (G.Droppable ? "true" : "false")
           << " entropy_threshold=" << G.EntropyThreshold
           << " max_hints_per_loop=" << G.MaxHintsPerLoop
           << " min_trip_count=" << G.MinTripCount << "\n";
}

/// Load `{"sites":[{"site_id":N,"entropy":F,...}]}` (DESIGN.md section 4.1).
/// Site ids are positional: analyze/profile/emit must run over the same module
/// for them to line up.  A mismatch degrades ranking quality only; it can
/// never produce wrong code.
void loadProfile(DenseMap<unsigned, double> &EntropyById) {
  if (HGProfile.empty())
    return;
  ErrorOr<std::unique_ptr<MemoryBuffer>> BufOrErr =
      MemoryBuffer::getFile(HGProfile);
  if (!BufOrErr) {
    errs() << "[hint-gather] warning: cannot read profile '" << HGProfile
           << "': " << BufOrErr.getError().message()
           << " -- ranking statically\n";
    return;
  }
  Expected<json::Value> Parsed = json::parse((*BufOrErr)->getBuffer());
  if (!Parsed) {
    consumeError(Parsed.takeError());
    errs() << "[hint-gather] warning: profile '" << HGProfile
           << "' is not valid JSON -- ranking statically\n";
    return;
  }
  const json::Object *O = Parsed->getAsObject();
  if (!O)
    return;
  const json::Array *Sites = O->getArray("sites");
  if (!Sites)
    return;
  for (const json::Value &V : *Sites) {
    const json::Object *S = V.getAsObject();
    if (!S)
      continue;
    auto Id = S->getInteger("site_id");
    auto E = S->getNumber("entropy");
    if (!Id || !E || *Id < 0)
      continue;
    EntropyById[static_cast<unsigned>(*Id)] = clampVal<double>(*E, 0.0, 1.0);
  }
  if (HGVerbose)
    errs() << "[hint-gather] loaded " << EntropyById.size()
           << " profiled entropies from " << HGProfile << "\n";
}

//===----------------------------------------------------------------------===//
// Candidate sites
//===----------------------------------------------------------------------===//

struct Candidate {
  unsigned Id = 0;

  // IR handles.  Everything SCEV-derived is snapshotted into the plain fields
  // below during detection so that nothing needs re-querying after mutation.
  Function *F = nullptr;
  Loop *L = nullptr;
  LoadInst *IdxLoad = nullptr; // B[i]
  LoadInst *DepLoad = nullptr; // A[B[i]]
  GetElementPtrInst *Gep = nullptr;
  Value *Base = nullptr; // A

  bool IdxSigned = false;   // index reached the GEP through an sext
  uint64_t ElemSize = 0;    // bytes, element of A
  unsigned Shift = 0;       // log2(ElemSize), 0..3
  int64_t IdxStepBytes = 0; // constant step of &B[i] per iteration

  bool ScevAffine = false; // dependent-load pointer is an affine AddRec
  int64_t TripCount = -1;  // -1 == not statically known
  double Entropy = -1.0;   // -1 == unknown (no profile available)
  double Score = 0.0;

  bool Eligible = true;
  std::string SkipReason;
  bool Emitted = false;

  // Report strings, snapshotted at detection time.
  std::string FuncName = "<unknown>";
  std::string LoopName = "<unknown>";
  std::string Source = "<unknown>:0";
  std::string BaseName = "<anon>";
  std::string IdxName = "<anon>";

  void skip(StringRef Why) {
    if (Eligible) {
      Eligible = false;
      SkipReason = Why.str();
    }
  }
  std::string pcSymbol() const { return "__hg_site_" + std::to_string(Id); }
};

/// Walk back from a GEP index through the casts DESIGN.md section 4.1 allows
/// (sext/zext/trunc) looking for the index load.  Deliberately conservative:
/// anything else returns null and the site is simply not a candidate.
LoadInst *stripToIndexLoad(Value *V, bool &Signed) {
  unsigned Depth = 0;
  while (V && Depth++ < 8) {
    if (auto *LI = dyn_cast<LoadInst>(V))
      return LI;
    auto *CI = dyn_cast<CastInst>(V);
    if (!CI)
      return nullptr;
    switch (CI->getOpcode()) {
    case Instruction::SExt:
      Signed = true;
      break;
    case Instruction::ZExt:
    case Instruction::Trunc:
      break;
    default:
      return nullptr;
    }
    V = CI->getOperand(0);
  }
  return nullptr;
}

std::string nameOf(const Value *V) {
  if (!V)
    return "<null>";
  if (V->hasName())
    return V->getName().str();
  return "<anon>";
}

std::string sourceOf(const Instruction *I) {
  if (!I)
    return "<unknown>:0";
  DebugLoc DL = I->getDebugLoc();
  if (!DL)
    return "<unknown>:0";
  if (DILocation *Loc = DL.get())
    return (Loc->getFilename() + ":" + Twine(Loc->getLine())).str();
  return "<unknown>:0";
}

/// Everything we create inherits a DebugLoc, otherwise the verifier complains
/// about calls without !dbg inside functions that carry debug info.
void copyDebugLoc(Instruction *New, Instruction *Model) {
  if (!New || !Model)
    return;
  DebugLoc DL = Model->getDebugLoc();
  if (DL) {
    New->setDebugLoc(DL);
    return;
  }
  if (Function *F = Model->getFunction())
    if (DISubprogram *SP = F->getSubprogram())
      New->setDebugLoc(
          DILocation::get(F->getContext(), /*Line=*/0, /*Column=*/0, SP));
}

/// Deterministic static ranking used when no profile is available.  Higher is
/// better; the value is intentionally in [0,1] so it is directly comparable
/// with a profiled entropy.
double staticScore(const Candidate &C, const Genome &G) {
  double S = 0.5;
  if (C.TripCount < 0) {
    S += 0.20; // runtime trip count -> usually the hot, long loop
  } else {
    double Denom = static_cast<double>(8 * G.MinTripCount + 1);
    double Frac = static_cast<double>(C.TripCount) / Denom;
    S += 0.30 * (Frac > 1.0 ? 1.0 : Frac);
  }
  if (C.ElemSize >= 4)
    S += 0.10;
  if (C.L && C.L->getLoopDepth() > 1)
    S += 0.05;
  return S > 1.0 ? 1.0 : S;
}

//===----------------------------------------------------------------------===//
// The pass implementation
//===----------------------------------------------------------------------===//

class HintGatherImpl {
public:
  HintGatherImpl(Module &M, ModuleAnalysisManager &AM) : M(M), AM(AM) {}

  /// Returns true if the IR was modified.
  bool run();

private:
  void collect(Function &F, LoopInfo &LI, ScalarEvolution &SE);
  void examineLoop(Function &F, Loop *L, ScalarEvolution &SE,
                   std::vector<Candidate> &Out);
  void rankAndAppend(std::vector<Candidate> &Local);
  bool emitSite(Candidate &C);
  bool instrumentSite(Candidate &C);
  void writeReport();

  Module &M;
  ModuleAnalysisManager &AM;
  Genome G;
  Mode Md = Mode::Analyze;
  DenseMap<unsigned, double> ProfiledEntropy;
  std::vector<Candidate> Sites;
  unsigned NextId = 0;
  bool TargetIsRISCV = false;
};

bool HintGatherImpl::run() {
  Md = parseMode(HGMode);
  loadGenome(G);
  loadProfile(ProfiledEntropy);

  // Module::getTargetTriple() returns `const std::string &` on LLVM 17..20.
  // TODO(llvm21+): it returns `const Triple &` there and this becomes
  //   TargetIsRISCV = M.getTargetTriple().isRISCV();
  // Symptom if wrong: "no viable conversion from 'const Triple' to
  // 'llvm::StringRef'" on the next statement.
  {
    StringRef T(M.getTargetTriple());
    TargetIsRISCV = T.substr(0, 5) == "riscv";
  }

  FunctionAnalysisManager &FAM =
      AM.getResult<FunctionAnalysisManagerModuleProxy>(M).getManager();

  for (Function &F : M) {
    if (F.isDeclaration() || F.hasOptNone())
      continue;
    LoopInfo &LI = FAM.getResult<LoopAnalysis>(F);
    ScalarEvolution &SE = FAM.getResult<ScalarEvolutionAnalysis>(F);
    collect(F, LI, SE);
  }

  // Mutate only after all detection is finished: inserting instructions while
  // walking loop bodies would invalidate the iterators we are standing on.
  bool Changed = false;
  for (Candidate &C : Sites) {
    if (!C.Eligible)
      continue;
    if (Md == Mode::Emit) {
      Changed |= emitSite(C);
    } else if (Md == Mode::Profile) {
      Changed |= instrumentSite(C);
    } else if (Md == Mode::Analyze && HGEmitSiteLabels && C.DepLoad) {
      LLVMContext &Ctx = M.getContext();
      IRBuilder<> B(C.DepLoad);
      std::string Sym = C.pcSymbol();
      std::string LabelAsm =
          ".ifndef " + Sym + "\n.globl " + Sym + "\n" + Sym + ":\n.endif\n";
      FunctionType *LblTy =
          FunctionType::get(Type::getVoidTy(Ctx), /*isVarArg=*/false);
      InlineAsm *LblAsm = InlineAsm::get(LblTy, LabelAsm, /*Constraints=*/"",
                                         /*hasSideEffects=*/true,
                                         /*isAlignStack=*/false);
      CallInst *Lbl = B.CreateCall(LblTy, LblAsm);
      copyDebugLoc(Lbl, C.DepLoad);
      Changed = true;
    }
  }

  writeReport();

  if (HGVerbose) {
    unsigned NumOk = 0, NumEmitted = 0;
    for (const Candidate &C : Sites) {
      NumOk += C.Eligible ? 1u : 0u;
      NumEmitted += C.Emitted ? 1u : 0u;
    }
    errs() << "[hint-gather] " << Sites.size() << " candidate(s), " << NumOk
           << " eligible, " << NumEmitted << " emitted\n";
  }
  return Changed;
}

void HintGatherImpl::collect(Function &F, LoopInfo &LI, ScalarEvolution &SE) {
  for (Loop *L : LI.getLoopsInPreorder()) {
    if (!L)
      continue;
    // Only innermost loops: a gather idiom "in" an outer loop is really in
    // whatever inner loop contains it, and considering both double-counts it.
    if (!L->isInnermost())
      continue;

    std::vector<Candidate> Local;
    examineLoop(F, L, SE, Local);
    if (!Local.empty())
      rankAndAppend(Local);
  }
}

void HintGatherImpl::examineLoop(Function &F, Loop *L, ScalarEvolution &SE,
                                 std::vector<Candidate> &Out) {
  const DataLayout &DL = M.getDataLayout();

  for (BasicBlock *BB : L->blocks()) {
    if (!BB)
      continue;
    for (Instruction &I : *BB) {
      auto *DepLoad = dyn_cast<LoadInst>(&I);
      if (!DepLoad || !DepLoad->isSimple())
        continue;

      auto *Gep = dyn_cast<GetElementPtrInst>(DepLoad->getPointerOperand());
      if (!Gep)
        continue;

      // load -> getelementptr -> load: find the GEP index that is (a cast of)
      // a load.
      bool IdxSigned = false;
      LoadInst *IdxLoad = nullptr;
      for (Use &U : Gep->indices()) {
        bool S = false;
        if (LoadInst *Cand = stripToIndexLoad(U.get(), S)) {
          IdxLoad = Cand;
          IdxSigned = S;
          break;
        }
      }
      if (!IdxLoad || IdxLoad == DepLoad)
        continue;

      Candidate C;
      C.F = &F;
      C.L = L;
      C.IdxLoad = IdxLoad;
      C.DepLoad = DepLoad;
      C.Gep = Gep;
      C.Base = Gep->getPointerOperand();
      C.IdxSigned = IdxSigned;
      C.FuncName = F.getName().str();
      C.LoopName = L->getHeader() ? nameOf(L->getHeader()) : "<no-header>";
      C.Source = sourceOf(DepLoad);
      C.BaseName = nameOf(getUnderlyingObject(C.Base));
      C.IdxName =
          nameOf(getUnderlyingObject(IdxLoad->getPointerOperand())) + "[i]";

      // ---- SCEV filter #1: reject affine dependent addresses --------------
      // DESIGN.md section 4.1: "Reject the site if getSCEV(gep_ptr) is an
      // affine SCEVAddRecExpr for the loop (the hardware stride prefetcher
      // already covers it)."
      if (SE.isSCEVable(Gep->getType())) {
        const SCEV *DepPtrSCEV = SE.getSCEV(Gep);
        if (const auto *AR = dyn_cast<SCEVAddRecExpr>(DepPtrSCEV))
          if (AR->getLoop() == L && AR->isAffine())
            C.ScevAffine = true;
      }
      if (C.ScevAffine)
        C.skip("affine_addrec_covered_by_stride_prefetcher");

      // ---- SCEV filter #2: the index stream must itself be affine ---------
      // Only then is &B[i+d] computable ahead of time, which is exactly what
      // the chase variant needs (DESIGN.md section 1.2).
      if (C.Eligible) {
        if (!L->contains(IdxLoad)) {
          C.skip("index_load_outside_loop");
        } else if (!IdxLoad->isSimple()) {
          C.skip("index_load_not_simple");
        } else {
          Value *IdxPtr = IdxLoad->getPointerOperand();
          const SCEVAddRecExpr *AR = nullptr;
          if (IdxPtr && SE.isSCEVable(IdxPtr->getType()))
            AR = dyn_cast<SCEVAddRecExpr>(SE.getSCEV(IdxPtr));
          if (!AR || AR->getLoop() != L || !AR->isAffine()) {
            C.skip("index_pointer_not_affine_addrec");
          } else if (const auto *StepC =
                         dyn_cast<SCEVConstant>(AR->getStepRecurrence(SE))) {
            const APInt &V = StepC->getAPInt();
            if (V.getSignificantBits() > 64)
              C.skip("addrec_step_too_wide");
            else
              C.IdxStepBytes = V.getSExtValue();
          } else {
            // A non-constant step would need a full SCEVExpander to
            // materialise &B[i+d].  Skipping keeps this pass total; see
            // README.md "Known restrictions".
            C.skip("non_constant_addrec_step");
          }
        }
      }

      // ---- element size / shift -------------------------------------------
      if (C.Eligible) {
        TypeSize TS = DL.getTypeStoreSize(DepLoad->getType());
        if (TS.isScalable()) {
          C.skip("scalable_element_type");
        } else {
          C.ElemSize = TS.getFixedValue();
          switch (C.ElemSize) {
          case 1:
            C.Shift = 0;
            break;
          case 2:
            C.Shift = 1;
            break;
          case 4:
            C.Shift = 2;
            break;
          case 8:
            C.Shift = 3;
            break;
          default:
            C.skip("unsupported_elem_size");
            break;
          }
        }
      }

      // ---- the index value must be an integer we can widen to XLEN --------
      if (C.Eligible && !IdxLoad->getType()->isIntegerTy())
        C.skip("index_load_not_integer");

      // ---- the base must be a pointer -------------------------------------
      if (C.Eligible && (!C.Base || !C.Base->getType()->isPointerTy()))
        C.skip("base_not_pointer");

      // ---- trip count ------------------------------------------------------
      unsigned TC = SE.getSmallConstantTripCount(L);
      if (TC == 0) {
        // Unknown: still eligible, reported as -1 (DESIGN.md section 4.1's
        // estimated_trip_count is an int; -1 means "not statically known").
        C.TripCount = -1;
      } else {
        C.TripCount = static_cast<int64_t>(TC);
        if (C.TripCount < G.MinTripCount)
          C.skip("trip_count_below_min");
      }

      Out.push_back(C);
    }
  }
}

void HintGatherImpl::rankAndAppend(std::vector<Candidate> &Local) {
  // Ids are assigned in a fixed traversal order so that a profile produced by
  // an earlier -hg-mode=profile run over the same module maps onto the same
  // sites.
  for (Candidate &C : Local) {
    C.Id = NextId++;
    auto It = ProfiledEntropy.find(C.Id);
    if (It != ProfiledEntropy.end())
      C.Entropy = It->second;
    C.Score = (C.Entropy >= 0.0) ? C.Entropy : staticScore(C, G);
    // entropy_threshold is only meaningful when a real profile exists.
    if (C.Eligible && C.Entropy >= 0.0 && C.Entropy < G.EntropyThreshold)
      C.skip("entropy_below_threshold");
  }

  // Keep at most max_hints_per_loop eligible sites, best score first.
  SmallVector<Candidate *, 8> Order;
  for (Candidate &C : Local)
    if (C.Eligible)
      Order.push_back(&C);
  std::stable_sort(Order.begin(), Order.end(),
                   [](const Candidate *A, const Candidate *B) {
                     return A->Score > B->Score;
                   });
  for (size_t I = 0, E = Order.size(); I != E; ++I)
    if (static_cast<int64_t>(I) >= G.MaxHintsPerLoop)
      Order[I]->skip("exceeds_max_hints_per_loop");

  for (Candidate &C : Local)
    Sites.push_back(C);
}

bool HintGatherImpl::instrumentSite(Candidate &C) {
  if (!C.DepLoad || !C.Gep)
    return false;
  LLVMContext &Ctx = M.getContext();
  IRBuilder<> B(C.DepLoad);

  Type *I32 = Type::getInt32Ty(Ctx);
  Type *I64 = Type::getInt64Ty(Ctx);
  FunctionCallee Fn = M.getOrInsertFunction("__hg_profile_access",
                                            Type::getVoidTy(Ctx), I32, I64);

  Value *Addr = B.CreatePtrToInt(C.Gep, I64, "hg.addr");
  if (auto *AI = dyn_cast<Instruction>(Addr))
    copyDebugLoc(AI, C.DepLoad);
  CallInst *Call = B.CreateCall(Fn, {ConstantInt::get(I32, C.Id), Addr});
  copyDebugLoc(Call, C.DepLoad);
  return true;
}

bool HintGatherImpl::emitSite(Candidate &C) {
  if (!C.DepLoad || !C.IdxLoad || !C.Base)
    return false;

  if (!TargetIsRISCV) {
    // `.insn r 0x0b, ...` only assembles for RISC-V.  Emitting it anywhere
    // else would turn a search-loop experiment into a build failure.
    C.skip("non_riscv_target");
    return false;
  }

  LLVMContext &Ctx = M.getContext();
  const DataLayout &DL = M.getDataLayout();
  IRBuilder<> B(C.DepLoad);

  Type *I32 = Type::getInt32Ty(Ctx);
  Type *I64 = Type::getInt64Ty(Ctx);
  Type *VoidTy = Type::getVoidTy(Ctx);

  Value *Rs2 = nullptr;
  if (G.isChase()) {
    // Chase form: rs2 = &B[i + hint_distance].
    //
    // &B[i] is the index load's pointer, whose SCEV we already proved to be an
    // affine AddRec with constant step C.IdxStepBytes, so the lookahead
    // address is just a byte offset from it.  A plain i8 GEP keeps this
    // opaque-pointer clean.
    //
    // Deliberately NOT inbounds: near the end of the array the lookahead
    // address legitimately runs past the object, and `inbounds` would make it
    // poison.  Computing an out-of-range address here is architecturally
    // harmless BY DESIGN -- the hint never faults, writes no register and no
    // memory, and is defined to be removable without changing the program
    // (DESIGN.md section 1.3 items 1-2 and 4).  That is exactly why no bounds
    // guard is emitted: a guard would cost a branch in the hot loop to protect
    // against something the ISA already defines as safe; the worst case is one
    // wasted prefetch.
    Value *IdxPtr = C.IdxLoad->getPointerOperand();
    if (!IdxPtr || !IdxPtr->getType()->isPointerTy()) {
      C.skip("index_pointer_not_pointer");
      return false;
    }
    // Saturating: a pathological genome must not overflow the offset.
    // Use the index element size (`sizeof(B[0])`) with the sign of
    // `C.IdxStepBytes` so that when the loop is unrolled by `fanout` (e.g.
    // `unroll_count(8)` where `IdxStepBytes == 8 * sizeof(B[0])`),
    // `hint_distance` still means `D` elements ahead (`&B[i + D]`) rather than
    // `8 * D` elements ahead (which would evict prefetched lines from L1D).
    int64_t ByteOff = 0;
    {
      const int64_t Limit = 1 << 20;
      int64_t D = G.HintDistance;
      TypeSize IdxTS = DL.getTypeStoreSize(C.IdxLoad->getType());
      int64_t ElemBytes = IdxTS.isScalable()
                              ? std::abs(C.IdxStepBytes)
                              : static_cast<int64_t>(IdxTS.getFixedValue());
      if (ElemBytes <= 0)
        ElemBytes = 4;
      int64_t S = (C.IdxStepBytes < 0) ? -ElemBytes : ElemBytes;
      if (D > Limit || S > Limit || S < -Limit)
        ByteOff = 0;
      else
        ByteOff = D * S;
    }
    Type *IdxTy = DL.getIntPtrType(IdxPtr->getType());
    Value *Off = ConstantInt::get(IdxTy, static_cast<uint64_t>(ByteOff),
                                  /*IsSigned=*/true);
    Value *LA = B.CreateGEP(B.getInt8Ty(), IdxPtr, Off, "hg.lookahead");
    if (auto *LAI = dyn_cast<Instruction>(LA))
      copyDebugLoc(LAI, C.DepLoad);
    Rs2 = LA;
  } else {
    // Value form: rs2 = the index value itself.
    //
    // LIMITATION: the architecturally intended operand is B[i + hint_distance]
    // (DESIGN.md section 1.2), but materialising it here would need an extra
    // *architectural* load -- exactly the IQ/LSQ cost the proposal exists to
    // avoid, and it would contaminate the measurement.  We therefore pass
    // B[i], the value already loaded this iteration.  Consequence: the value
    // variant only buys lookahead when the loop is unrolled or software
    // pipelined, so expect the search to prefer "chase".  This is a
    // deliberate, documented handicap of variant 0, not an oversight.
    Rs2 = C.IdxSigned ? B.CreateSExtOrTrunc(C.IdxLoad, I64, "hg.idx")
                      : B.CreateZExtOrTrunc(C.IdxLoad, I64, "hg.idx");
    if (auto *RI = dyn_cast<Instruction>(Rs2))
      copyDebugLoc(RI, C.DepLoad);
  }

  if (!Rs2)
    return false;

  // The template and operand order are byte-for-byte what
  // include/hint_gather.h expands to (%N becomes $N in LLVM IR asm):
  //   .insn r 0x0b, <funct3>, <funct7>, x0, <base>, <rs2>
  SmallVector<Type *, 4> ArgTys = {C.Base->getType(), Rs2->getType(), I32, I32};
  FunctionType *FTy = FunctionType::get(VoidTy, ArgTys, /*isVarArg=*/false);
  InlineAsm *IA = InlineAsm::get(FTy, ".insn r 0x0b, $2, $3, x0, $0, $1",
                                 /*Constraints=*/"r,r,i,i",
                                 /*hasSideEffects=*/true,
                                 /*isAlignStack=*/false);
  SmallVector<Value *, 4> Args = {C.Base, Rs2,
                                  ConstantInt::get(I32, G.funct3()),
                                  ConstantInt::get(I32, G.funct7(C.Shift))};
  CallInst *Hint = B.CreateCall(FTy, IA, Args);
  copyDebugLoc(Hint, C.DepLoad);
  // `hasSideEffects` above is the only thing keeping the hint alive: it writes
  // no register and no memory, so without it DCE would delete every hint.  No
  // "~{memory}" clobber is claimed, because the hint is defined not to order
  // against any memory operation (DESIGN.md section 1.3 item 3) and a bogus
  // clobber would needlessly block surrounding optimisation.

  // pc_symbol: a local label immediately before the demand load, which the
  // ChampSim node resolves to a PC with nm/objdump (DESIGN.md section 4.1).
  // The .ifndef guard makes it idempotent should a later duplication pass
  // (tail duplication, machine unroll) copy the block.
  //
  // TODO: if the integrated assembler rejects `.ifndef`, the symptom is
  // "error: unknown directive" pointing at `.ifndef`; drop the guard lines and
  // accept a possible "symbol '__hg_site_N' is already defined" on duplicated
  // blocks.
  std::string Sym = C.pcSymbol();
  std::string LabelAsm = ".ifndef " + Sym + "\n" + Sym + ":\n.endif\n";
  FunctionType *LblTy = FunctionType::get(VoidTy, /*isVarArg=*/false);
  InlineAsm *LblAsm = InlineAsm::get(LblTy, LabelAsm, /*Constraints=*/"",
                                     /*hasSideEffects=*/true,
                                     /*isAlignStack=*/false);
  CallInst *Lbl = B.CreateCall(LblTy, LblAsm);
  copyDebugLoc(Lbl, C.DepLoad);

  C.Emitted = true;
  return true;
}

void HintGatherImpl::writeReport() {
  std::string Path =
      HGReport.empty() ? std::string("hint_sites.json") : std::string(HGReport);
  std::error_code EC;
  raw_fd_ostream OS(Path, EC, sys::fs::OF_Text);
  if (EC) {
    errs() << "[hint-gather] error: cannot write report '" << Path
           << "': " << EC.message() << "\n";
    return;
  }

  json::OStream J(OS, /*IndentSize=*/2);
  J.object([&] {
    J.attribute("genome_hash", G.Hash);
    J.attributeArray("sites", [&] {
      for (const Candidate &C : Sites) {
        J.object([&] {
          J.attribute("site_id", static_cast<int64_t>(C.Id));
          J.attribute("function", C.FuncName);
          J.attribute("loop_header", C.LoopName);
          J.attribute("source", C.Source);
          J.attribute("base_value", C.BaseName);
          J.attribute("index_load", C.IdxName);
          J.attribute("elem_size", static_cast<int64_t>(C.ElemSize));
          J.attribute("scev_affine", C.ScevAffine);
          J.attribute("estimated_trip_count", C.TripCount);
          J.attribute("entropy", C.Entropy);
          J.attribute("emitted", C.Emitted);
          J.attribute("pc_symbol", C.pcSymbol());
          // Additive field required by the repair loop: the empty string means
          // the site passed every filter.  See README.md.
          J.attribute("skip_reason", C.SkipReason);
        });
      }
    });
  });
  OS << "\n";
}

//===----------------------------------------------------------------------===//
// New-PM wrapper
//===----------------------------------------------------------------------===//

struct HintGatherPass : PassInfoMixin<HintGatherPass> {
  PreservedAnalyses run(Module &M, ModuleAnalysisManager &AM) {
    HintGatherImpl Impl(M, AM);
    bool Changed = Impl.run();
    return Changed ? PreservedAnalyses::none() : PreservedAnalyses::all();
  }
  static bool isRequired() { return true; }
};

} // namespace

//===----------------------------------------------------------------------===//
// Plugin registration
//===----------------------------------------------------------------------===//

static void registerCallbacks(PassBuilder &PB) {
  // Explicit: `opt -load-pass-plugin=libHintGather.so -passes=hint-gather`.
  PB.registerPipelineParsingCallback(
      [](StringRef Name, ModulePassManager &MPM,
         ArrayRef<PassBuilder::PipelineElement>) {
        if (Name == "hint-gather") {
          MPM.addPass(HintGatherPass());
          return true;
        }
        return false;
      });

  // Implicit: `clang -fpass-plugin=libHintGather.so`.  OptimizerLast runs
  // after LICM and the vectoriser, so the IR we match is close to the IR that
  // will actually be emitted.
#if LLVM_VERSION_MAJOR >= 20
  // The extension-point callbacks gained a ThinOrFullLTOPhase parameter in
  // LLVM 20.  Symptom if this guard is wrong for your LLVM: "no matching
  // function for call to 'registerOptimizerLastEPCallback'".
  PB.registerOptimizerLastEPCallback(
      [](ModulePassManager &MPM, OptimizationLevel, ThinOrFullLTOPhase) {
        MPM.addPass(HintGatherPass());
      });
#else
  PB.registerOptimizerLastEPCallback(
      [](ModulePassManager &MPM, OptimizationLevel) {
        MPM.addPass(HintGatherPass());
      });
#endif
}

// The plugin entry point MUST have default ELF visibility, because clang and
// opt find it with dlsym().  CMakeLists.txt deliberately sets
// CXX_VISIBILITY_PRESET=hidden (a plugin should not export its internals into
// the host process), and that preset applies to this symbol too unless it is
// exempted here.
//
// The failure mode is silent and very misleading: with the symbol hidden,
// `nm -D --defined-only libHintGather.so` lists NOTHING, clang reports
// "Plugin entry point not found ... Is this a legacy plugin?", and opt says
// "Failed to load passes ... Request ignored." and then carries on to exit 0.
// A build that emits no hints then looks like a pass bug rather than a link
// bug.  If you ever see zero hint sites, check this symbol first:
//
//     nm -D --defined-only libHintGather.so | grep llvmGetPassPluginInfo
//
// LLVM_EXTERNAL_VISIBILITY does not exist before LLVM 13 and is a no-op in
// some configurations, so pin the attribute directly.
#if defined(_WIN32)
#define HG_PLUGIN_EXPORT
#else
#define HG_PLUGIN_EXPORT __attribute__((visibility("default")))
#endif

extern "C" HG_PLUGIN_EXPORT LLVM_ATTRIBUTE_WEAK ::llvm::PassPluginLibraryInfo
llvmGetPassPluginInfo() {
  return {LLVM_PLUGIN_API_VERSION, "HintGather", LLVM_VERSION_STRING,
          registerCallbacks};
}
