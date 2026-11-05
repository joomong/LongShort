#pragma once

#include "module_registration.h"
#include "simulation_data.h"
#include "meta.h"
#include <map>
#include <memory>
#include <string>
#include <utility>
#include <vector>

struct XmlElement {
    std::string tag;
    std::map<std::string, std::string> attribs;
    std::vector<XmlElement> children;  // for <choice>
};

// Parse XML string → list of top-level elements.
// Wraps in <alpha>...</alpha> if needed (multiple sibling tags).
// Throws std::runtime_error on parse failure.
std::vector<XmlElement> parse_xml(const std::string& xml);

// Human-readable XML fragments, with two-space indentation and a trailing newline.
// Keeps comments, attribute values and sibling elements; throws on invalid XML.
std::string format_xml(const std::string& xml);

// Extract editor/compiler operations from a saved <alpha><pipeline> document.
// Legacy operation fragments are accepted unchanged. Metadata never becomes a module.
std::string pipeline_xml(const std::string& xml);

using Op = std::pair<std::string, std::map<std::string, std::string>>;
using Pipeline = std::vector<Op>;

// Hard cap to prevent unbounded combinatorial explosion during XML expansion.
static constexpr int MAX_ALPHA_COUNT = 5000;

// Expand elements into variant pipelines.
// - Pipe:  "A|B|C" in any attribute value → that many variants
// - Range: "20...10...60" or "20...60" → list of string ints
// - <choice>: children are alternatives (no attributes accepted)
// Throws std::runtime_error if variant count exceeds MAX_ALPHA_COUNT.
std::vector<Pipeline> expand_pipelines(const std::vector<XmlElement>& elements);

class Module;
struct DataPanel;

// Parse an expression string into a Module tree.
// All created modules are appended to `owned`; caller manages lifetime.
// Returns a non-owning pointer to the root (output) module.
// Allowed identifiers: fields registered in the DataPanel, plus derived returns. A wide dataset field
//   is `<dataset>.<field>` (one dot, no whitespace, field part [A-Za-z0-9_]+, e.g. AFL.10DMACD) and is
//   always a data field, never a function; an unknown bare name that exactly one wide dataset carries
//   gets a "(did you mean '<dataset>.<name>'?)" hint.
// Allowed operators: +, -, *, / and parentheses
// Functions (identifier followed by '(', whitespace allowed): abs(x) log(x) exp(x) sqrt(x) sign(x) power(x, p) signedpower(x, p);
//   x is any expression, p a numeric literal with optional '-'. Each is Chain(x, MUnary<Op>), same Op as the
//   chain tag. Any other name followed by '(' is an error; a name not followed by '(' is a data field.
// Numeric literals produce MConstant modules.
// Special: "returns" is the panel field when it has one (thin-v1), else close / Delay(close, 1) - 1.0
// Throws std::runtime_error on invalid syntax or unknown identifier.
Module* parse_expr(const std::string& expr,
                   const DataPanel* panel,
                   std::vector<std::unique_ptr<Module>>& owned);

struct MMetaSource;  // src/modules/MetaSource.h
struct MetaSet;

struct CompiledAlpha {
    std::vector<std::unique_ptr<Module>> modules;  // owns all modules
    std::vector<Module*> pipeline;                  // top-level execution order
    std::vector<const Module*> chain_starts;        // pipeline modules that begin a <data>/<meta> chain
    Module* output;                                 // last module (non-owning)
    std::string source_xml;                         // expanded per-alpha XML
    std::vector<MetaSet> meta_sets;                 // <meta> alpha sets, in pipeline order
};

struct MetaSet {
    std::string name;                               // the expr value
    std::vector<std::string> member_paths;          // as given by the resolver, in order
    std::vector<CompiledAlpha> members;             // one compiled alpha per member, same order
    MMetaSource* source = nullptr;                  // module in `pipeline` yielding the combined row
    CombineSpec combine;                            // finalizer tag (<ew/>, <mvo>, ...); implicit -> Ew
};

// (path, xml) pairs for a meta name; throws std::runtime_error for unknown name / empty folder.
using MetaResolver = std::function<std::vector<std::pair<std::string, std::string>>(const std::string& name)>;

// First panel bar with a valid signal when computation starts at `start`; needs no init().
// Precondition: call before prefetch or bind, which release <meta> members.
unsigned first_valid_bar(const CompiledAlpha& alpha, unsigned start);

// Compile a single pipeline into a CompiledAlpha.
CompiledAlpha compile_pipeline(const Pipeline& ops, const DataPanel* panel,
                               const std::vector<uint8_t>* universe = nullptr,
                               const MetaResolver* resolver = nullptr);

// Compile XML string → list of compiled alphas (one per expanded variant).
// `universe` overrides the panel's default signal-date mask for rank/zscore/neutralize.
std::vector<CompiledAlpha> compile_xml(const std::string& xml, const DataPanel* panel,
                                       const std::vector<uint8_t>* universe = nullptr,
                                       const MetaResolver* resolver = nullptr);

namespace range_parser {
std::string trim_copy(const std::string& text);
std::vector<std::string> parse_range_notation(const std::string& text);
}
