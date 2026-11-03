#include "alpha_search.h"
#include "xml_compiler.h"
#include <sstream>
#include <stdexcept>
#include <regex>
#include <limits>
#include <algorithm>
#include <unordered_set>
#include <cmath>

// ---------------------------------------------------------------------------
// total_combinations
// ---------------------------------------------------------------------------
size_t SearchSpace::total_combinations() const {
    if (genes.empty()) return 1;
    size_t total = 1;
    for (const auto& g : genes) {
        if (g.values.empty()) return 0;
        if (total > std::numeric_limits<size_t>::max() / g.values.size())
            return std::numeric_limits<size_t>::max();
        total *= g.values.size();
    }
    return total;
}

// ---------------------------------------------------------------------------
// decode
// Rebuild XML by substituting gene values into the template.
std::string SearchSpace::decode(const std::vector<int>& indices) const {
    if (template_xml.empty()) {
        // No template stored — return empty
        return "";
    }

    if (indices.size() != genes.size()) {
        throw std::invalid_argument("decode: indices size mismatch");
    }

    std::string result = template_xml;
    for (size_t i = 0; i < genes.size(); ++i) {
        std::string placeholder = "__GENE_" + std::to_string(i) + "__";
        const std::string& val = genes[i].values[indices[i]];
        size_t pos = 0;
        while ((pos = result.find(placeholder, pos)) != std::string::npos) {
            result.replace(pos, placeholder.size(), val);
            pos += val.size();
        }
    }
    return result;
}

// ---------------------------------------------------------------------------
// extract_search_space
//
// Strategy: scan attribute values for pipe-separated lists and range syntax.
// For each expansion dimension found, record its possible values and replace
// the original value in the template with a __GENE_N__ placeholder.
//
// We do NOT use expand_pipelines() because that explodes all combinations;
// instead we enumerate dimensions independently.
//
// Supported patterns:
//   pipe:   "10|20|30"           → Gene with values ["10","20","30"]
//   range:  "20...10...60"       → Gene with values ["20","30","40","50","60"]
//           "20...60"            → Gene with values ["20","21",...,"60"]
//   <choice>: handled up front as one "pipeline" gene (see below)
// ---------------------------------------------------------------------------

namespace {

// Split string by delimiter, return parts
std::vector<std::string> split(const std::string& s, char delim) {
    std::vector<std::string> parts;
    std::stringstream ss(s);
    std::string item;
    while (std::getline(ss, item, delim)) {
        item = range_parser::trim_copy(item);
        if (std::find(parts.begin(), parts.end(), item) == parts.end()) parts.push_back(item);
    }
    return parts;
}

// Try to parse range syntax "start...step...end" or "start...end".
// Returns empty vector if not a range pattern.
std::vector<std::string> try_parse_range(const std::string& val) {
    return range_parser::parse_range_notation(val);
}

// Check if a value looks like a pipe list (contains '|')
bool is_pipe(const std::string& val) {
    return val.find('|') != std::string::npos;
}

std::string escape_attribute(const std::string& value) {
    std::string escaped;
    for (char c : value) {
        if (c == '&') escaped += "&amp;";
        else if (c == '<') escaped += "&lt;";
        else if (c == '>') escaped += "&gt;";
        else if (c == '"') escaped += "&quot;";
        else escaped += c;
    }
    return escaped;
}

// Attribute order is stable and XML expressions retain escaping.
std::string element_to_xml(const XmlElement& elem) {
    std::string s = "<" + elem.tag;
    for (const auto& kv : elem.attribs) {
        s += " " + kv.first + "=\"" + escape_attribute(kv.second) + "\"";
    }
    if (elem.children.empty()) {
        s += "/>";
    } else {
        s += ">";
        for (const auto& child : elem.children) {
            s += element_to_xml(child);
        }
        s += "</" + elem.tag + ">";
    }
    return s;
}

} // namespace

SearchSpace extract_search_space(const std::string& xml_template) {
    SearchSpace space;

    // Parse the XML
    std::vector<XmlElement> elements = parse_xml(xml_template);

    // Structural choices use the compiler's existing expansion contract
    // (plain <choice> alternatives). Ordinary parameter searches stay
    // factored so large Cartesian spaces are never expanded up front.
    if (std::any_of(elements.begin(), elements.end(), [](const XmlElement& element) {
            return element.tag == "choice";
        })) {
        SearchSpace::Gene gene;
        gene.description = "pipeline";
        std::unordered_set<std::string> seen;
        for (const auto& pipeline : expand_pipelines(elements)) {
            std::string xml;
            for (const auto& op : pipeline) {
                XmlElement element;
                element.tag = op.first;
                element.attribs = op.second;
                if (!xml.empty()) xml += "\n";
                xml += element_to_xml(element);
            }
            if (seen.insert(xml).second) gene.values.push_back(std::move(xml));
        }
        if (gene.values.empty()) throw std::invalid_argument("XML has no candidate pipelines");
        space.genes.push_back(std::move(gene));
        space.template_xml = "__GENE_0__";
        return space;
    }

    // We will build a modified copy of elements with placeholders,
    // tracking genes as we go.
    int gene_idx = 0;

    // Process each element's attributes
    std::vector<XmlElement> modified = elements;
    for (auto& elem : modified) {
        // Skip structural tags (choice)
        if (elem.tag == "choice") continue;

        for (auto& kv : elem.attribs) {
            const std::string& val = kv.second;

            // Try range first
            auto range_vals = try_parse_range(val);
            if (!range_vals.empty()) {
                SearchSpace::Gene gene;
                gene.description = elem.tag + "." + kv.first;
                gene.values = std::move(range_vals);
                space.genes.push_back(std::move(gene));

                kv.second = "__GENE_" + std::to_string(gene_idx) + "__";
                ++gene_idx;
                continue;
            }

            // Try pipe
            if (is_pipe(val)) {
                auto pipe_vals = split(val, '|');
                for (auto& value : pipe_vals) value = escape_attribute(value);
                SearchSpace::Gene gene;
                gene.description = elem.tag + "." + kv.first;
                gene.values = std::move(pipe_vals);
                space.genes.push_back(std::move(gene));

                kv.second = "__GENE_" + std::to_string(gene_idx) + "__";
                ++gene_idx;
                continue;
            }
        }
    }

    // Reconstruct the placeholder template string, one module per line like
    // the compiler's expanded XML.
    std::string tmpl;
    for (const auto& elem : modified) {
        if (!tmpl.empty()) tmpl += "\n";
        tmpl += element_to_xml(elem);
    }

    space.template_xml = std::move(tmpl);

    return space;
}

GAEngine::GAEngine(const std::string& xml_template, const SearchSpace& space, const GAConfig& config)
    : xml_template_(xml_template), space_(space), config_(config) {
    if (!config_.population_size || !config_.generations || !config_.first_search || !config_.stop_alphas)
        throw std::invalid_argument("GA first search, round size and stop limits must be positive");
    if (!(config_.random_fraction >= 0 && config_.random_fraction <= 1))
        throw std::invalid_argument("GA random fraction must be between zero and one");
}
GAResult GAEngine::run(EvalFunc evaluate) { return run_impl(std::move(evaluate), {}); }
GAResult GAEngine::run_detailed(EvalFunc evaluate, const GAEvents& events) {
    return run_impl(std::move(evaluate), events);
}
GAResult GAEngine::run_streaming(EvalFunc evaluate, ProgressCallback cb) {
    GAEvents events;
    events.round_end = [cb](unsigned round, const std::string& xml, double score) { cb(round - 1, xml, score); };
    return run_impl(std::move(evaluate), events);
}
GAEngine::Individual GAEngine::random_individual(std::mt19937& rng) const {
    Individual individual;
    for (const auto& gene : space_.genes) {
        std::uniform_int_distribution<int> draw(0, static_cast<int>(gene.values.size()) - 1);
        individual.push_back(draw(rng));
    }
    return individual;
}
GAEngine::Individual GAEngine::crossover(const Individual& a, const Individual& b, std::mt19937& rng) const {
    Individual child;
    std::uniform_real_distribution<double> probability(0, 1);
    for (size_t i = 0; i < a.size(); ++i) child.push_back(probability(rng) < .5 ? a[i] : b[i]);
    return child;
}
GAEngine::Individual GAEngine::mutate(const Individual& individual, std::mt19937& rng) const {
    Individual child = individual;
    std::uniform_real_distribution<double> probability(0, 1);
    for (size_t i = 0; i < space_.genes.size(); ++i)
        if (probability(rng) < config_.mutation_rate) {
            std::uniform_int_distribution<int> draw(0, static_cast<int>(space_.genes[i].values.size()) - 1);
            child[i] = draw(rng);
        }
    return child;
}
int GAEngine::tournament_select(const std::vector<double>& scores, std::mt19937& rng) const {
    std::uniform_int_distribution<int> draw(0, static_cast<int>(scores.size()) - 1);
    int best = draw(rng);
    for (unsigned i = 1; i < config_.tournament_size; ++i) {
        int candidate = draw(rng);
        if (scores[candidate] > scores[best]) best = candidate;
    }
    return best;
}
GAResult GAEngine::run_impl(EvalFunc evaluate, const GAEvents& events) {
    ParallelEvaluator evaluator(config_.n_threads, std::move(evaluate));
    std::mt19937 rng(config_.seed);
    std::unordered_set<std::string> scheduled;
    struct Parent { Individual genes; double score; size_t id; };
    std::vector<Parent> parents;
    GAResult result;
    double best_score = -std::numeric_limits<double>::infinity();
    const size_t total = space_.total_combinations();
    size_t enumeration_cursor = 0;
    auto enumerate = [&](size_t index) {
        Individual genes(space_.genes.size());
        for (size_t i = genes.size(); i > 0; --i) {
            const size_t radix = space_.genes[i - 1].values.size();
            genes[i - 1] = static_cast<int>(index % radix);
            index /= radix;
        }
        return genes;
    };
    for (unsigned round = 1; round <= config_.generations; ++round) {
        if (events.cancelled && events.cancelled()) throw EvaluationCancelled();
        const size_t remaining = total - scheduled.size();
        const size_t target = std::min({static_cast<size_t>(round == 1 ? config_.first_search : config_.population_size),
            static_cast<size_t>(config_.stop_alphas) - scheduled.size(), remaining});
        if (!target) break;
        std::vector<Individual> genes;
        std::vector<GACandidate> candidates;
        auto add = [&](Individual individual, const std::string& origin) {
            const std::string xml = space_.decode(individual);
            if (!scheduled.insert(xml).second) return false;
            GACandidate candidate;
            candidate.id = scheduled.size() - 1;
            candidate.round = round;
            candidate.xml = xml;
            candidate.origin = origin;
            genes.push_back(std::move(individual));
            candidates.push_back(std::move(candidate));
            return true;
        };
        if (target == remaining) {
            while (candidates.size() < target && enumeration_cursor < total)
                add(enumerate(enumeration_cursor++), "exhaustive");
        } else {
            std::stable_sort(parents.begin(), parents.end(), [](const Parent& a, const Parent& b) {
                return a.score == b.score ? a.id < b.id : a.score > b.score;
            });
            const size_t pool = std::min<size_t>(parents.size(), config_.population_size);
            std::vector<double> scores;
            for (size_t i = 0; i < pool; ++i) scores.push_back(parents[i].score);
            const size_t random_target = round == 1 || scores.empty() ? target
                : static_cast<size_t>(std::ceil(target * config_.random_fraction));
            std::uniform_real_distribution<double> probability(0, 1);
            for (size_t attempt = 0; candidates.size() < target && attempt < target * 64; ++attempt) {
                if (events.cancelled && events.cancelled()) throw EvaluationCancelled();
                if (candidates.size() < random_target) add(random_individual(rng), "random");
                else {
                    const auto& a = parents[tournament_select(scores, rng)].genes;
                    const auto& b = parents[tournament_select(scores, rng)].genes;
                    add(mutate(probability(rng) < config_.crossover_rate ? crossover(a, b, rng) : a, rng), "genetic");
                }
            }
            for (size_t attempt = 0; candidates.size() < target && attempt < target * 64; ++attempt)
                add(random_individual(rng), "random");
            while (candidates.size() < target && enumeration_cursor < total)
                add(enumerate(enumeration_cursor++), "exhaustive");
        }
        if (candidates.empty()) break;
        if (events.round_start) events.round_start(round, candidates.size());
        std::vector<std::string> xmls;
        for (const auto& candidate : candidates) xmls.push_back(candidate.xml);
        if (events.round_prepare) events.round_prepare(xmls);
        const auto evaluations = evaluator.evaluate_results(xmls, [&](size_t index, const EvaluationResult& value) {
            candidates[index].fitness = value.fitness;
            candidates[index].error = value.error;
            if (events.candidate) events.candidate(candidates[index]);
        }, events.cancelled);
        std::string round_best;
        double round_score = -std::numeric_limits<double>::infinity();
        // Parent selection uses candidate order, not completion order.
        for (size_t i = 0; i < candidates.size(); ++i) {
            ++result.evaluated;
            if (!evaluations[i].error.empty()) continue;
            const double score = evaluations[i].fitness;
            parents.push_back({genes[i], score, candidates[i].id});
            if (score > round_score) { round_score = score; round_best = candidates[i].xml; }
            if (score > best_score) { best_score = score; result.best_xml = candidates[i].xml; }
        }
        result.history.emplace_back(round_best, round_score);
        if (events.round_end) events.round_end(round, round_best, round_score);
        if (scheduled.size() >= total) { result.stop_reason = "exhausted"; break; }
        if (scheduled.size() >= config_.stop_alphas) { result.stop_reason = "stop_alphas"; break; }
    }
    if (result.stop_reason.empty()) result.stop_reason = "stop_round";
    return result;
}
