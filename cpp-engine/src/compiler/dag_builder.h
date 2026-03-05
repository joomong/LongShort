#pragma once

#include "expansion.h"
#include "module.h"
#include <memory>
#include <vector>
#include <string>
#include <map>
#include <unordered_map>
#include <functional>
#include <stdexcept>

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
    void register_module(const std::string& tag, ModuleFactory factory);
    std::unique_ptr<Module> create(const std::string& tag,
                                   const std::map<std::string, std::string>& attribs,
                                   const DataPanel* panel) const;
    static ModuleRegistry& default_registry();
};

struct CompiledAlpha {
    std::vector<std::unique_ptr<Module>> modules;  // owns all modules
    std::vector<Module*> pipeline;                  // top-level execution order
    Module* output;                                 // last module (non-owning)
    std::string source_xml;                         // expanded per-alpha XML
};

// Compile a single pipeline into a CompiledAlpha.
CompiledAlpha compile_pipeline(const Pipeline& ops, const DataPanel* panel);

// Compile XML string → list of compiled alphas (one per expanded variant).
std::vector<CompiledAlpha> compile_xml(const std::string& xml, const DataPanel* panel);
