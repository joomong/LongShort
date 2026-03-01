#include "dag_builder.h"
#include "xml_parser.h"
#include "expansion.h"
#include "expr_parser.h"
#include "../modules/Data.h"
#include "../modules/Delay.h"
#include "../modules/TsMean.h"
#include "../modules/TsRank.h"
#include "../modules/TsZscore.h"
#include "../modules/Decay.h"
#include "../modules/Event.h"
#include "../modules/Rank.h"
#include "../modules/Zscore.h"
#include "../modules/Ops.h"
#include "../modules/Constant.h"
#include <stdexcept>

// ---------------------------------------------------------------------------
// Helper: get required int/double/bool attrib with default
// ---------------------------------------------------------------------------

static int get_int(const std::map<std::string, std::string>& a,
                   const std::string& key, int def = 0) {
    auto it = a.find(key);
    if (it == a.end()) return def;
    return std::stoi(it->second);
}

static double get_double(const std::map<std::string, std::string>& a,
                         const std::string& key, double def = 0.0) {
    auto it = a.find(key);
    if (it == a.end()) return def;
    return std::stod(it->second);
}

static bool get_bool(const std::map<std::string, std::string>& a,
                     const std::string& key, bool def = false) {
    auto it = a.find(key);
    if (it == a.end()) return def;
    const std::string& v = it->second;
    return (v == "true" || v == "1" || v == "yes");
}

// ---------------------------------------------------------------------------
// ModuleRegistry (STAR-style: default-construct + field injection)
// ---------------------------------------------------------------------------

void ModuleRegistry::register_module(const std::string& tag, ModuleFactory factory) {
    factories_[tag] = std::move(factory);
}

std::unique_ptr<Module> ModuleRegistry::create(
    const std::string& tag,
    const std::map<std::string, std::string>& attribs,
    const DataPanel* panel) const
{
    (void)panel;
    auto it = factories_.find(tag);
    if (it == factories_.end())
        throw CompileError("Unknown module tag: '" + tag + "'");
    return it->second(attribs, panel);
}

ModuleRegistry& ModuleRegistry::default_registry() {
    static ModuleRegistry reg = [](){
        ModuleRegistry r;

        r.register_module("tsmean", [](const auto& a, const auto*) {
            auto m = std::make_unique<MTsMean>();
            m->days = IntParameter(get_int(a, "days", 20));
            return m;
        });

        r.register_module("tszscore", [](const auto& a, const auto*) {
            auto m = std::make_unique<MTsZscore>();
            m->days = IntParameter(get_int(a, "days", 20));
            m->ddof = IntParameter(get_int(a, "ddof", 1));
            m->min_count = IntParameter(get_int(a, "min_count", -1));
            m->eps = FloatParameter(get_double(a, "eps", 1e-12));
            return m;
        });

        r.register_module("tsrank", [](const auto& a, const auto*) {
            auto m = std::make_unique<MTsRank>();
            m->days = IntParameter(get_int(a, "days", 20));
            m->pct = IntParameter(get_bool(a, "pct", true) ? 1 : 0);
            m->ascending = IntParameter(get_bool(a, "ascending", false) ? 1 : 0);
            m->min_count = IntParameter(get_int(a, "min_count", -1));
            return m;
        });

        r.register_module("decay", [](const auto& a, const auto*) {
            auto m = std::make_unique<MDecay>();
            m->days = IntParameter(get_int(a, "days", 20));
            return m;
        });

        r.register_module("delay", [](const auto& a, const auto*) {
            auto m = std::make_unique<MDelay>();
            m->days = IntParameter(get_int(a, "days", 1));
            return m;
        });

        r.register_module("rank", [](const auto& a, const auto*) {
            auto m = std::make_unique<MRank>();
            m->pct = IntParameter(get_bool(a, "pct", true) ? 1 : 0);
            return m;
        });

        r.register_module("zscore", [](const auto& a, const auto*) {
            auto m = std::make_unique<MZscore>();
            m->ddof = IntParameter(get_int(a, "ddof", 1));
            m->min_count = IntParameter(get_int(a, "min_count", 2));
            m->eps = FloatParameter(get_double(a, "eps", 1e-12));
            return m;
        });

        return r;
    }();
    return reg;
}

// ---------------------------------------------------------------------------
// compile_pipeline (STAR-style: field injection, no constructors)
// ---------------------------------------------------------------------------

CompiledAlpha compile_pipeline(const Pipeline& ops, const DataPanel* panel) {
    if (ops.empty())
        throw CompileError("Pipeline is empty");

    CompiledAlpha result;
    std::map<std::string, Module*> signal_registry;

    if (ops[0].first != "data")
        throw CompileError("First pipeline element must be 'data', got '" + ops[0].first + "'");

    const auto& data_attribs = ops[0].second;

    std::string expr_str = "close";
    {
        auto it = data_attribs.find("expr");
        if (it != data_attribs.end()) expr_str = it->second;
    }

    Module* current = parse_expr(expr_str, panel, result.modules);

    // Apply delay if present on the data tag
    {
        auto it = data_attribs.find("delay");
        if (it != data_attribs.end()) {
            int d = std::stoi(it->second);
            if (d != 0) {
                auto delay = std::make_unique<MDelay>();
                delay->days = IntParameter(d);
                current = delay.get();
                result.modules.push_back(std::move(delay));
            }
        }
    }

    const ModuleRegistry& reg = ModuleRegistry::default_registry();

    for (size_t i = 1; i < ops.size(); ++i) {
        const std::string& tag    = ops[i].first;
        const auto&        attribs = ops[i].second;

        if (tag == "signal") {
            auto it = attribs.find("id");
            if (it == attribs.end())
                throw CompileError("<signal> tag missing 'id' attribute");
            signal_registry[it->second] = current;
            continue;
        }

        if (tag == "event") {
            std::string sig_id, trig_id;
            {
                auto it = attribs.find("signal");
                if (it != attribs.end()) sig_id = it->second;
            }
            {
                auto it = attribs.find("trigger");
                if (it != attribs.end()) trig_id = it->second;
            }

            Module* sig_mod  = nullptr;
            Module* trig_mod = nullptr;

            if (!sig_id.empty()) {
                auto it = signal_registry.find(sig_id);
                if (it == signal_registry.end())
                    throw CompileError("event: unknown signal id '" + sig_id + "'");
                sig_mod = it->second;
            } else {
                sig_mod = current;
            }

            if (!trig_id.empty()) {
                auto it = signal_registry.find(trig_id);
                if (it == signal_registry.end())
                    throw CompileError("event: unknown trigger id '" + trig_id + "'");
                trig_mod = it->second;
            } else {
                throw CompileError("event: 'trigger' attribute required");
            }

            auto mod = std::make_unique<MEvent>();
            mod->signal_module  = sig_mod;
            mod->trigger_module = trig_mod;
            mod->threshold      = FloatParameter(get_double(attribs, "threshold", 0.0));
            mod->maxHolddays    = IntParameter(get_int(attribs, "maxholddays", 5));
            mod->betAll         = IntParameter(get_bool(attribs, "betall", true) ? 1 : 0);
            current = mod.get();
            result.modules.push_back(std::move(mod));
            continue;
        }

        auto mod = reg.create(tag, attribs, panel);
        current = mod.get();
        result.modules.push_back(std::move(mod));
    }

    result.output = current;
    return result;
}

// ---------------------------------------------------------------------------
// compile_xml
// ---------------------------------------------------------------------------

std::vector<CompiledAlpha> compile_xml(const std::string& xml, const DataPanel* panel) {
    auto elements = parse_xml(xml);
    auto pipelines = expand_pipelines(elements);

    std::vector<CompiledAlpha> result;
    result.reserve(pipelines.size());
    for (auto& pipe : pipelines)
        result.push_back(compile_pipeline(pipe, panel));
    return result;
}
