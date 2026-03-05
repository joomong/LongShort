#include "dag_builder.h"
#include "xml_parser.h"
#include "expansion.h"
#include "expr_parser.h"
#include "../data/data_panel.h"
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
#include "../modules/SignalCache.h"
#include <stdexcept>
#include <sstream>
#include <limits>
#include <cctype>

// ---------------------------------------------------------------------------
// Helper: get required int/double/bool attrib with default
// ---------------------------------------------------------------------------

static int get_int(const std::map<std::string, std::string>& a,
                   const std::string& key, int def = 0) {
    auto it = a.find(key);
    if (it == a.end()) return def;
    return std::stoi(it->second);
}

static int panel_time_limit(const DataPanel* panel) {
    if (panel == nullptr)
        throw CompileError("DataPanel is required for compile-time range validation");
    if (panel->T > static_cast<unsigned>(std::numeric_limits<int>::max()))
        throw CompileError("DataPanel T exceeds supported range for compile-time validation");
    return static_cast<int>(panel->T);
}

static std::string make_range(int min_value, int max_value) {
    return "[" + std::to_string(min_value) + ", " + std::to_string(max_value) + "]";
}

static std::string trim_ascii_whitespace(const std::string& value) {
    size_t begin = 0;
    while (begin < value.size() &&
           std::isspace(static_cast<unsigned char>(value[begin]))) {
        ++begin;
    }

    size_t end = value.size();
    while (end > begin &&
           std::isspace(static_cast<unsigned char>(value[end - 1]))) {
        --end;
    }

    return value.substr(begin, end - begin);
}

static int parse_bounded_int(const std::string& tag,
                             const std::string& attribute,
                             const std::string& raw_value,
                             int min_value,
                             int max_value) {
    int parsed = 0;
    const std::string trimmed = trim_ascii_whitespace(raw_value);
    try {
        size_t consumed = 0;
        parsed = std::stoi(trimmed, &consumed);
        if (consumed != trimmed.size()) {
            throw CompileError(
                "Invalid value for <" + tag + "> attribute '" + attribute + "': '" + raw_value +
                "' (expected integer in range " + make_range(min_value, max_value) + ")");
        }
    } catch (const std::exception&) {
        throw CompileError(
            "Invalid value for <" + tag + "> attribute '" + attribute + "': '" + raw_value +
            "' (expected integer in range " + make_range(min_value, max_value) + ")");
    }

    if (parsed < min_value || parsed > max_value) {
        throw CompileError(
            "Invalid value for <" + tag + "> attribute '" + attribute + "': '" + raw_value +
            "' (expected range " + make_range(min_value, max_value) + ")");
    }
    return parsed;
}

static int get_bounded_int(const std::map<std::string, std::string>& attribs,
                           const std::string& tag,
                           const std::string& attribute,
                           int default_value,
                           int min_value,
                           int max_value) {
    auto it = attribs.find(attribute);
    if (it == attribs.end())
        return parse_bounded_int(
            tag, attribute, std::to_string(default_value), min_value, max_value);
    return parse_bounded_int(tag, attribute, it->second, min_value, max_value);
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

static std::string xml_escape(const std::string& v) {
    std::string out;
    out.reserve(v.size());
    for (char c : v) {
        switch (c) {
        case '&': out += "&amp;"; break;
        case '<': out += "&lt;"; break;
        case '>': out += "&gt;"; break;
        case '"': out += "&quot;"; break;
        case '\'': out += "&apos;"; break;
        default: out += c; break;
        }
    }
    return out;
}

static std::string pipeline_to_xml(const Pipeline& ops) {
    std::ostringstream oss;
    for (size_t i = 0; i < ops.size(); ++i) {
        const auto& op = ops[i];
        oss << "<" << op.first;
        for (const auto& kv : op.second)
            oss << " " << kv.first << "=\"" << xml_escape(kv.second) << "\"";
        oss << "/>";
        if (i + 1 < ops.size())
            oss << "\n";
    }
    return oss.str();
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
    auto it = factories_.find(tag);
    if (it == factories_.end())
        throw CompileError("Unknown module tag: '" + tag + "'");
    return it->second(attribs, panel);
}

ModuleRegistry& ModuleRegistry::default_registry() {
    static ModuleRegistry reg = [](){
        ModuleRegistry r;

        r.register_module("tsmean", [](const auto& a, const auto* panel) {
            auto m = std::make_unique<MTsMean>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "tsmean", "days", 20, 1, max_days));
            return m;
        });

        r.register_module("tszscore", [](const auto& a, const auto* panel) {
            auto m = std::make_unique<MTsZscore>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "tszscore", "days", 20, 1, max_days));
            m->ddof = IntParameter(get_int(a, "ddof", 1));
            m->min_count = IntParameter(get_int(a, "min_count", -1));
            m->eps = FloatParameter(get_double(a, "eps", 1e-12));
            return m;
        });

        r.register_module("tsrank", [](const auto& a, const auto* panel) {
            auto m = std::make_unique<MTsRank>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "tsrank", "days", 20, 1, max_days));
            m->pct = IntParameter(get_bool(a, "pct", true) ? 1 : 0);
            m->ascending = IntParameter(get_bool(a, "ascending", false) ? 1 : 0);
            m->min_count = IntParameter(get_int(a, "min_count", -1));
            return m;
        });

        r.register_module("decay", [](const auto& a, const auto* panel) {
            auto m = std::make_unique<MDecay>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "decay", "days", 20, 1, max_days));
            return m;
        });

        r.register_module("delay", [](const auto& a, const auto* panel) {
            auto m = std::make_unique<MDelay>();
            const int max_delay = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "delay", "days", 1, 0, max_delay));
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
    // cache_registry: MSignalSave* for input/signal/event cache restoration
    std::map<std::string, MSignalSave*> cache_registry;
    unsigned data_chain_count = 1;

    if (ops[0].first != "data")
        throw CompileError("First pipeline element must be 'data', got '" + ops[0].first + "'");

    const auto& data_attribs = ops[0].second;

    std::string expr_str = "close";
    {
        auto it = data_attribs.find("expr");
        if (it != data_attribs.end()) expr_str = it->second;
    }

    Module* current = parse_expr(expr_str, panel, result.modules);
    result.pipeline.push_back(current);

    // Apply delay if present on the data tag
    {
        auto it = data_attribs.find("delay");
        if (it != data_attribs.end()) {
            const int max_lookback = panel_time_limit(panel);
            int d = parse_bounded_int("data", "delay", it->second, 0, max_lookback);
            if (d != 0) {
                auto delay = std::make_unique<MDelay>();
                delay->days = IntParameter(d);
                current = delay.get();
                result.pipeline.push_back(current);
                result.modules.push_back(std::move(delay));
            }
        }
    }

    const ModuleRegistry& reg = ModuleRegistry::default_registry();

    for (size_t i = 1; i < ops.size(); ++i) {
        const std::string& tag    = ops[i].first;
        const auto&        attribs = ops[i].second;

        if (tag == "data") {
            ++data_chain_count;
            std::string expr = "close";
            {
                auto it = attribs.find("expr");
                if (it != attribs.end()) expr = it->second;
            }
            current = parse_expr(expr, panel, result.modules);
            result.pipeline.push_back(current);

            {
                auto it = attribs.find("delay");
                if (it != attribs.end()) {
                    const int max_lookback = panel_time_limit(panel);
                    int d = parse_bounded_int("data", "delay", it->second, 0, max_lookback);
                    if (d != 0) {
                        auto delay_mod = std::make_unique<MDelay>();
                        delay_mod->days = IntParameter(d);
                        current = delay_mod.get();
                        result.pipeline.push_back(current);
                        result.modules.push_back(std::move(delay_mod));
                    }
                }
            }
            continue;
        }

        if (tag == "signal") {
            auto it = attribs.find("id");
            if (it == attribs.end())
                throw CompileError("<signal> tag missing 'id' attribute");
            const std::string& sig_id = it->second;

            auto save = std::make_unique<MSignalSave>();
            save->label = sig_id;
            MSignalSave* save_ptr = save.get();
            result.pipeline.push_back(save_ptr);
            result.modules.push_back(std::move(save));
            cache_registry[sig_id] = save_ptr;

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

            const std::vector<float>* sig_cache = nullptr;
            const std::vector<float>* trig_cache = nullptr;
            bool use_current_signal = false;

            if (!sig_id.empty()) {
                auto it = cache_registry.find(sig_id);
                if (it == cache_registry.end())
                    throw CompileError("event: unknown signal id '" + sig_id + "'");
                sig_cache = &it->second->cache;
            } else {
                if (data_chain_count > 1) {
                    throw CompileError(
                        "event: 'signal' attribute required after data chain restart");
                }
                use_current_signal = true;
            }

            if (!trig_id.empty()) {
                auto it = cache_registry.find(trig_id);
                if (it == cache_registry.end())
                    throw CompileError("event: unknown trigger id '" + trig_id + "'");
                trig_cache = &it->second->cache;
            } else {
                throw CompileError("event: 'trigger' attribute required");
            }

            auto mod = std::make_unique<MEvent>();
            mod->signal_cache   = sig_cache;
            mod->trigger_cache  = trig_cache;
            mod->useCurrentSignal = IntParameter(use_current_signal ? 1 : 0);
            mod->threshold      = FloatParameter(get_double(attribs, "threshold", 0.0));
            mod->maxHolddays    = IntParameter(get_int(attribs, "maxholddays", 5));
            mod->betAll         = IntParameter(get_bool(attribs, "betall", true) ? 1 : 0);
            current = mod.get();
            result.pipeline.push_back(current);
            result.modules.push_back(std::move(mod));
            continue;
        }

        {
            auto inp_it = attribs.find("input");
            if (inp_it != attribs.end()) {
                const std::string& input_id = inp_it->second;
                auto cache_it = cache_registry.find(input_id);
                if (cache_it == cache_registry.end())
                    throw CompileError("input: unknown signal id '" + input_id + "'");
                auto load = std::make_unique<MSignalLoad>();
                load->source = cache_it->second;
                result.pipeline.push_back(load.get());
                result.modules.push_back(std::move(load));
            }
        }

        auto mod = reg.create(tag, attribs, panel);
        current = mod.get();
        result.pipeline.push_back(current);
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
    for (auto& pipe : pipelines) {
        auto compiled = compile_pipeline(pipe, panel);
        compiled.source_xml = pipeline_to_xml(pipe);
        result.push_back(std::move(compiled));
    }
    return result;
}
