#pragma once

#include "expansion.h"
#include "module.h"
#include "combine_spec.h"
#include <memory>
#include <vector>
#include <string>
#include <map>
#include <unordered_map>
#include <functional>
#include <stdexcept>
#include <utility>
#include <cstdint>

struct DataPanel;

struct CompileError : public std::runtime_error {
    using std::runtime_error::runtime_error;
};

using ModuleFactory = std::function<std::unique_ptr<Module>(
    const std::map<std::string, std::string>& attribs,
    const DataPanel* panel)>;

class ModuleRegistry {
    std::unordered_map<std::string, ModuleFactory> factories_;
public:
    // Duplicate tags are errors; discovered modules cannot replace built-ins.
    void register_module(const std::string& tag, ModuleFactory factory);
    std::unique_ptr<Module> create(const std::string& tag,
                                   const std::map<std::string, std::string>& attribs,
                                   const DataPanel* panel) const;
    static ModuleRegistry& default_registry();
};

// Generated from the configured modules directory during the build.
void register_discovered_modules(ModuleRegistry& registry);

struct MMetaSource;  // src/modules/MetaSource.h
struct MetaSet;

struct CompiledAlpha {
    std::vector<std::unique_ptr<Module>> modules;  // owns all modules
    std::vector<Module*> pipeline;                  // top-level execution order
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

// Compile a single pipeline into a CompiledAlpha.
CompiledAlpha compile_pipeline(const Pipeline& ops, const DataPanel* panel,
                               const std::vector<uint8_t>* universe = nullptr,
                               const MetaResolver* resolver = nullptr);

// Compile XML string → list of compiled alphas (one per expanded variant).
// `universe` overrides the panel's default signal-date mask for rank/zscore/neutralize.
std::vector<CompiledAlpha> compile_xml(const std::string& xml, const DataPanel* panel,
                                       const std::vector<uint8_t>* universe = nullptr,
                                       const MetaResolver* resolver = nullptr);
