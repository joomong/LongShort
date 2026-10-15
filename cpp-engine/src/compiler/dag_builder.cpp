#include "dag_builder.h"
#include "xml_parser.h"
#include "expansion.h"
#include "expr_parser.h"
#include "module_attributes.h"
#include "../data/data_panel.h"
#include "../modules/Data.h"
#include "../modules/MetaSource.h"
#include "../modules/Delay.h"
#include "../modules/TsMean.h"
#include "../modules/TsRank.h"
#include "../modules/TsZscore.h"
#include "../modules/Decay.h"
#include "../modules/Event.h"
#include "../modules/Rank.h"
#include "../modules/Zscore.h"
#include "../modules/Neutralize.h"
#include "../modules/Ops.h"
#include "../modules/Constant.h"
#include "../modules/SignalCache.h"
#include <stdexcept>
#include <sstream>
#include <limits>
#include <cctype>

// ---------------------------------------------------------------------------
// Helpers: optional attribs with default, parsed strictly by module_attributes
// ---------------------------------------------------------------------------

static int opt_int(const std::map<std::string, std::string>& a, const std::string& tag,
                   const std::string& key, int def) {
    auto it = a.find(key);
    return it == a.end() ? def : module_attributes::parse_int(it->second, tag, key);
}

static double opt_float(const std::map<std::string, std::string>& a, const std::string& tag,
                        const std::string& key, double def) {
    auto it = a.find(key);
    return it == a.end() ? def : module_attributes::parse_float(it->second, tag, key);
}

static bool opt_bool(const std::map<std::string, std::string>& a, const std::string& tag,
                     const std::string& key, bool def) {
    auto it = a.find(key);
    return it == a.end() ? def : module_attributes::parse_bool(it->second, tag, key);
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
    if (!factories_.emplace(tag, std::move(factory)).second)
        throw CompileError("Duplicate module tag: '" + tag + "'");
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
            module_attributes::validate_keys(a, {"days"}, "tsmean");
            auto m = std::make_unique<MTsMean>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "tsmean", "days", 20, 1, max_days));
            return m;
        });

        r.register_module("tszscore", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"days", "ddof", "min_count", "eps"}, "tszscore");
            auto m = std::make_unique<MTsZscore>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "tszscore", "days", 20, 1, max_days));
            m->ddof = IntParameter(opt_int(a, "tszscore", "ddof", 1));
            m->min_count = IntParameter(opt_int(a, "tszscore", "min_count", -1));
            m->eps = FloatParameter(opt_float(a, "tszscore", "eps", 1e-12));
            return m;
        });

        r.register_module("tsrank", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"days", "pct", "min_count"}, "tsrank");
            auto m = std::make_unique<MTsRank>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "tsrank", "days", 20, 1, max_days));
            m->pct = IntParameter(opt_bool(a, "tsrank", "pct", true) ? 1 : 0);

            m->min_count = IntParameter(opt_int(a, "tsrank", "min_count", -1));
            return m;
        });

        r.register_module("decay", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"days"}, "decay");
            auto m = std::make_unique<MDecay>();
            const int max_days = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "decay", "days", 20, 1, max_days));
            return m;
        });

        r.register_module("delay", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"days"}, "delay");
            auto m = std::make_unique<MDelay>();
            const int max_delay = panel_time_limit(panel);
            m->days = IntParameter(get_bounded_int(a, "delay", "days", 1, 0, max_delay));
            return m;
        });

        r.register_module("rank", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"pct"}, "rank");
            auto m = std::make_unique<MRank>();
            m->panel = panel;
            m->pct = IntParameter(opt_bool(a, "rank", "pct", true) ? 1 : 0);
            return m;
        });

        r.register_module("zscore", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"ddof", "min_count", "eps"}, "zscore");
            auto m = std::make_unique<MZscore>();
            m->panel = panel;
            m->ddof = IntParameter(opt_int(a, "zscore", "ddof", 1));
            m->min_count = IntParameter(opt_int(a, "zscore", "min_count", 2));
            m->eps = FloatParameter(opt_float(a, "zscore", "eps", 1e-12));
            return m;
        });

        r.register_module("neutralize", [](const auto& a, const auto* panel) {
            module_attributes::validate_keys(a, {"group"}, "neutralize");
            auto it = a.find("group");
            if (it == a.end())
                throw CompileError(
                    "<neutralize> requires attribute 'group' (sector|industry)");
            // Trim like parse_bounded_int does for numeric attributes.
            const std::string group = trim_ascii_whitespace(it->second);
            if (group != "sector" && group != "industry")
                throw CompileError(
                    "Unsupported neutralize group '" + group +
                    "' (supported: sector, industry)");
            if (panel == nullptr)
                throw CompileError(
                    "DataPanel is required for <neutralize> group validation");
            // group name == panel field name for every supported group
            const std::string column = group;
            if (!panel->has_column(column))
                throw CompileError(
                    "neutralize group '" + group + "' needs data field '" + column +
                    "' (add base: {cache, groups} to the config)");
            auto m = std::make_unique<MNeutralize>();
            m->panel = panel;
            m->group = group;
            m->column_name = column;
            return m;
        });

        register_discovered_modules(r);
        return r;
    }();
    return reg;
}

// ---------------------------------------------------------------------------
// compile_pipeline (STAR-style: field injection, no constructors)
// ---------------------------------------------------------------------------

static bool contains_meta(const std::vector<XmlElement>& elements) {
    for (const auto& element : elements)
        if (element.tag == "meta" || contains_meta(element.children)) return true;
    return false;
}

// Set finalizer tags: they turn the pending <meta> SET into a SIGNAL (see combine_spec.h).
static bool is_combine_tag(const std::string& tag) {
    return tag == "ew" || tag == "mvo" || tag == "minvar" || tag == "erc" || tag == "ivol" ||
           tag == "spca" || tag == "glasso";
}

static unsigned parse_combine_uint(const std::map<std::string, std::string>& attribs, const std::string& tag,
                                   const std::string& key, int min_value, bool required, unsigned fallback) {
    auto it = attribs.find(key);
    if (it == attribs.end()) {
        if (required) throw CompileError("<" + tag + "> requires attribute '" + key + "'");
        return fallback;
    }
    const std::string expected = "integer >= " + std::to_string(min_value);
    int value = 0;
    try {
        value = module_attributes::parse_int(it->second, tag, key);
    } catch (const CompileError&) {
        throw module_attributes::detail::invalid_value(it->second, tag, key, expected.c_str());
    }
    if (value < min_value)
        throw module_attributes::detail::invalid_value(it->second, tag, key, expected.c_str());
    return static_cast<unsigned>(value);
}

static CombineSpec parse_combine_spec(const std::string& tag, const std::map<std::string, std::string>& attribs) {
    CombineSpec spec;
    if (tag == "ew") {
        module_attributes::validate_keys(attribs, {"sign"}, "ew", false);
        if (attribs.count("sign")) {
            spec.method = CombineMethod::EwSign;
            spec.days = parse_combine_uint(attribs, tag, "sign", 1, true, 0);
        }
        return spec;
    }
    if (tag == "mvo" || tag == "minvar") {
        module_attributes::validate_keys(attribs, {"days", "shrink", "rebalance"}, tag, false);
        spec.method = tag == "mvo" ? CombineMethod::Mvo : CombineMethod::MinVar;
        auto it = attribs.find("shrink");
        if (it != attribs.end()) {
            const std::string value = module_attributes::detail::trim(it->second);
            if (value == "lw") spec.shrink = ShrinkMode::LedoitWolf;
            else if (value == "cc") spec.shrink = ShrinkMode::ConstantCorrelation;
            else if (value == "oas") spec.shrink = ShrinkMode::Oas;
            else {
                const char* expected = "a number in [0,1] or lw|cc|oas";
                double delta = 0.0;
                try {
                    delta = module_attributes::parse_float(it->second, tag, "shrink");
                } catch (const CompileError&) {
                    throw module_attributes::detail::invalid_value(it->second, tag, "shrink", expected);
                }
                if (!(delta >= 0.0 && delta <= 1.0))
                    throw module_attributes::detail::invalid_value(it->second, tag, "shrink", expected);
                spec.delta = delta;
            }
        }
    } else if (tag == "erc" || tag == "ivol") {
        module_attributes::validate_keys(attribs, {"days", "rebalance"}, tag, false);
        spec.method = tag == "erc" ? CombineMethod::Erc : CombineMethod::Ivol;
    } else if (tag == "spca") {
        module_attributes::validate_keys(attribs, {"days", "k", "rebalance"}, tag, false);
        spec.method = CombineMethod::Spca;
        spec.k = parse_combine_uint(attribs, tag, "k", 1, true, 0);
    } else {  // glasso
        module_attributes::validate_keys(attribs, {"days", "alpha", "rebalance"}, tag, false);
        spec.method = CombineMethod::Glasso;
        auto it = attribs.find("alpha");
        if (it == attribs.end()) throw CompileError("<glasso> requires attribute 'alpha'");
        const char* expected = "number > 0";
        double alpha = 0.0;
        try {
            alpha = module_attributes::parse_float(it->second, tag, "alpha");
        } catch (const CompileError&) {
            throw module_attributes::detail::invalid_value(it->second, tag, "alpha", expected);
        }
        if (!(alpha > 0.0)) throw module_attributes::detail::invalid_value(it->second, tag, "alpha", expected);
        spec.alpha = alpha;
    }
    spec.days = parse_combine_uint(attribs, tag, "days", 2, true, 0);
    spec.rebalance = parse_combine_uint(attribs, tag, "rebalance", 1, false, 1);
    return spec;
}

// Compile the member alphas of <meta expr="NAME"> into a MetaSet (state SET until finalized).
static MetaSet compile_meta_set(const std::map<std::string, std::string>& attribs,
                                const DataPanel* panel,
                                const std::vector<uint8_t>* universe,
                                const MetaResolver* resolver) {
    module_attributes::validate_keys(attribs, {"expr"}, "meta", false);
    auto it = attribs.find("expr");
    if (it == attribs.end())
        throw CompileError("<meta> requires attribute 'expr'");
    if (resolver == nullptr || !*resolver)
        throw CompileError("meta_dir is not configured");
    MetaSet set;
    set.name = it->second;
    std::vector<std::pair<std::string, std::string>> listed;
    try {
        listed = (*resolver)(set.name);
    } catch (const CompileError&) {
        throw;
    } catch (const std::runtime_error& e) {
        throw CompileError(e.what());
    }
    const std::string prefix = "meta '" + set.name + "' member ";
    for (const auto& [path, xml] : listed) {
        std::vector<CompiledAlpha> compiled;
        try {
            if (contains_meta(parse_xml(xml)))
                throw CompileError(prefix + path + ": nested <meta> is not allowed");
            compiled = compile_xml(xml, panel, universe, nullptr);
        } catch (const CompileError& e) {
            const std::string msg = e.what();
            if (msg.rfind(prefix, 0) == 0) throw;
            throw CompileError(prefix + path + ": " + msg);
        } catch (const std::runtime_error& e) {
            throw CompileError(prefix + path + ": " + e.what());
        }
        if (compiled.size() != 1)
            throw CompileError(prefix + path + " must compile to exactly one alpha (got " +
                               std::to_string(compiled.size()) + ")");
        set.member_paths.push_back(path);
        set.members.push_back(std::move(compiled.front()));
    }
    return set;
}

CompiledAlpha compile_pipeline(const Pipeline& ops, const DataPanel* panel,
                               const std::vector<uint8_t>* universe,
                               const MetaResolver* resolver) {
    if (ops.empty())
        throw CompileError("Pipeline is empty");

    CompiledAlpha result;
    // cache_registry: MSignalSave* for input/signal/event cache restoration
    std::map<std::string, MSignalSave*> cache_registry;
    unsigned data_chain_count = 0;
    Module* current = nullptr;

    if (ops[0].first != "data" && ops[0].first != "meta")
        throw CompileError("First pipeline element must be 'data', got '" + ops[0].first + "'");
    const bool starts_with_meta = ops[0].first == "meta";

    // SET state: the last meta_sets entry has no source yet. Finalizing (explicit <ew/> or
    // implicit before any other op / at the end) appends its MMetaSource; state -> SIGNAL.
    bool pending_set = false;
    bool meta_chain_unnamed = false;  // current chain began with <meta>, no <signal id> yet
    auto finalize_set = [&](const CombineSpec& spec) {
        result.meta_sets.back().combine = spec;
        auto source = std::make_unique<MMetaSource>();
        source->name = result.meta_sets.back().name;
        result.meta_sets.back().source = source.get();
        current = source.get();
        result.pipeline.push_back(current);
        result.modules.push_back(std::move(source));
        pending_set = false;
    };

    const ModuleRegistry& reg = ModuleRegistry::default_registry();

    for (size_t i = 0; i < ops.size(); ++i) {
        const std::string& tag    = ops[i].first;
        const auto&        attribs = ops[i].second;

        if (is_combine_tag(tag)) {
            if (!pending_set)
                throw CompileError("<" + tag + "> requires an alpha set from <meta>");
            finalize_set(parse_combine_spec(tag, attribs));
            continue;
        }
        if (pending_set) finalize_set(CombineSpec{});

        // A <meta> chain left unnamed would run all its members for nothing.
        if ((tag == "meta" || tag == "data") && meta_chain_unnamed)
            throw CompileError("a <meta> chain must be named with <signal id> before another chain starts");
        if (tag == "signal") meta_chain_unnamed = false;

        if (tag == "meta") {
            meta_chain_unnamed = true;
            ++data_chain_count;
            result.meta_sets.push_back(compile_meta_set(attribs, panel, universe, resolver));
            pending_set = true;
            continue;
        }

        if (tag == "data") {
            if (starts_with_meta)
                throw CompileError("<data> cannot restart a pipeline that began with <meta>");
            ++data_chain_count;
            // A misspelt delay must not compile to delay 0 (same-bar look-ahead).
            module_attributes::validate_keys(attribs, {"expr", "delay"}, "data", false);
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
            module_attributes::validate_keys(attribs, {"id"}, "signal", false);
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
            module_attributes::validate_keys(
                attribs, {"signal", "trigger", "threshold", "maxholddays", "betall"}, "event", false);
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
            mod->threshold      = FloatParameter(opt_float(attribs, "event", "threshold", 0.0));
            mod->maxHolddays    = IntParameter(opt_int(attribs, "event", "maxholddays", 5));
            mod->betAll         = IntParameter(opt_bool(attribs, "event", "betall", true) ? 1 : 0);
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

    if (pending_set) finalize_set(CombineSpec{});
    result.output = current;
    return result;
}

// ---------------------------------------------------------------------------
// compile_xml
// ---------------------------------------------------------------------------

std::vector<CompiledAlpha> compile_xml(const std::string& xml, const DataPanel* panel,
                                       const std::vector<uint8_t>* universe,
                                       const MetaResolver* resolver) {
    auto elements = parse_xml(xml);
    auto pipelines = expand_pipelines(elements);

    std::vector<CompiledAlpha> result;
    result.reserve(pipelines.size());
    for (auto& pipe : pipelines) {
        auto compiled = compile_pipeline(pipe, panel, universe, resolver);
        compiled.source_xml = pipeline_to_xml(pipe);
        if (universe)
            for (auto& module : compiled.modules) {
                if (auto* m = dynamic_cast<MRank*>(module.get())) m->universe = universe;
                else if (auto* m = dynamic_cast<MZscore*>(module.get())) m->universe = universe;
                else if (auto* m = dynamic_cast<MNeutralize*>(module.get())) m->universe = universe;
            }
        result.push_back(std::move(compiled));
    }
    return result;
}
