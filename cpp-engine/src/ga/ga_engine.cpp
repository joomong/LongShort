#include "ga_engine.h"
#include "parallel_evaluator.h"

#include <algorithm>
#include <limits>
#include <stdexcept>
#include <thread>

// ---------------------------------------------------------------------------
// Constructor
// ---------------------------------------------------------------------------
GAEngine::GAEngine(const std::string& xml_template,
                   const SearchSpace& space,
                   const GAConfig& config)
    : xml_template_(xml_template), space_(space), config_(config)
{}

// ---------------------------------------------------------------------------
// Public entry points
// ---------------------------------------------------------------------------
GAResult GAEngine::run(EvalFunc evaluate) {
    return run_impl(evaluate, nullptr);
}

GAResult GAEngine::run_streaming(EvalFunc evaluate, ProgressCallback cb) {
    return run_impl(evaluate, &cb);
}

// ---------------------------------------------------------------------------
// Individual helpers
// ---------------------------------------------------------------------------
GAEngine::Individual GAEngine::random_individual(std::mt19937& rng) const {
    Individual ind;
    // real genes only (skip sentinel with empty values)
    for (const auto& g : space_.genes) {
        if (g.values.empty()) continue;
        std::uniform_int_distribution<int> dist(0, (int)g.values.size() - 1);
        ind.push_back(dist(rng));
    }
    return ind;
}

GAEngine::Individual GAEngine::crossover(const Individual& a,
                                         const Individual& b,
                                         std::mt19937& rng) const {
    // Uniform crossover: each gene independently chosen from a or b
    Individual child;
    child.reserve(a.size());
    std::uniform_real_distribution<double> prob(0.0, 1.0);
    for (size_t i = 0; i < a.size(); ++i) {
        child.push_back(prob(rng) < 0.5 ? a[i] : b[i]);
    }
    return child;
}

GAEngine::Individual GAEngine::mutate(const Individual& ind,
                                      std::mt19937& rng) const {
    Individual result = ind;
    std::uniform_real_distribution<double> prob(0.0, 1.0);

    size_t gene_pos = 0;
    for (const auto& g : space_.genes) {
        if (g.values.empty()) continue;  // sentinel
        if (prob(rng) < config_.mutation_rate) {
            std::uniform_int_distribution<int> dist(0, (int)g.values.size() - 1);
            result[gene_pos] = dist(rng);
        }
        ++gene_pos;
    }
    return result;
}

int GAEngine::tournament_select(const std::vector<double>& fitness,
                                 std::mt19937& rng) const {
    int n = (int)fitness.size();
    std::uniform_int_distribution<int> dist(0, n - 1);

    int best = dist(rng);
    for (unsigned i = 1; i < config_.tournament_size; ++i) {
        int candidate = dist(rng);
        if (fitness[candidate] > fitness[best]) {
            best = candidate;
        }
    }
    return best;
}

double GAEngine::get_fitness(const SummaryMetrics& m) const {
    const auto& metric = config_.fitness_metric;
    if (metric == "sharpe")    return m.sharpe;
    if (metric == "pnl")       return m.pnl;
    if (metric == "ann_ret")   return m.ann_ret;
    if (metric == "mdd")       return m.mdd;
    if (metric == "turnover")  return m.turnover;
    if (metric == "win_rate")  return m.win_rate;
    if (metric == "min_bet")   return m.min_bet;
    if (metric == "max_weight") return m.max_weight;
    if (metric == "margin")    return m.margin;
    // Default: sharpe
    return m.sharpe;
}

// ---------------------------------------------------------------------------
// Core GA loop
// ---------------------------------------------------------------------------
GAResult GAEngine::run_impl(EvalFunc evaluate, ProgressCallback* cb) {
    // Determine real gene count
    size_t n_genes = 0;
    for (const auto& g : space_.genes) {
        if (!g.values.empty()) ++n_genes;
    }

    // Edge case: no genes → single evaluation of the template as-is
    if (n_genes == 0) {
        GAResult result;
        result.best_xml = xml_template_;
        double fit = evaluate(xml_template_);
        // best_metrics left default (we only have fitness here)
        result.history.emplace_back(xml_template_, fit);
        return result;
    }

    unsigned n_threads = config_.n_threads;
    if (n_threads == 0) {
        n_threads = std::thread::hardware_concurrency();
        if (n_threads == 0) n_threads = 1;
    }

    // Build parallel evaluator wrapping the user's eval function
    ParallelEvaluator evaluator(n_threads, evaluate);

    std::mt19937 rng(config_.seed);

    // --- Initialize population ---
    std::vector<Individual> population(config_.population_size);
    for (auto& ind : population) {
        ind = random_individual(rng);
    }

    GAResult result;
    result.best_metrics = SummaryMetrics{};
    double best_fitness = -std::numeric_limits<double>::infinity();

    // --- Generation loop ---
    for (unsigned gen = 0; gen < config_.generations; ++gen) {
        // Decode all individuals → XML strings
        std::vector<std::string> xmls(config_.population_size);
        for (unsigned i = 0; i < config_.population_size; ++i) {
            xmls[i] = space_.decode(population[i]);
        }

        // Evaluate batch (parallel, with cache)
        std::vector<double> fitness = evaluator.evaluate_batch(xmls);

        // Find best in this generation
        int gen_best_idx = 0;
        for (unsigned i = 1; i < config_.population_size; ++i) {
            if (fitness[i] > fitness[gen_best_idx]) gen_best_idx = i;
        }
        double gen_best_fitness = fitness[gen_best_idx];
        const std::string& gen_best_xml = xmls[gen_best_idx];

        if (gen_best_fitness > best_fitness) {
            best_fitness = gen_best_fitness;
            result.best_xml = gen_best_xml;
            // best_metrics cannot be filled here without re-running the full
            // backtest — the EvalFunc returns only a scalar.  Leave it default.
        }

        result.history.emplace_back(gen_best_xml, gen_best_fitness);

        if (cb) {
            (*cb)(gen, gen_best_xml, gen_best_fitness);
        }

        // --- Build next generation ---
        std::vector<Individual> next_population;
        next_population.reserve(config_.population_size);

        // Elitism: carry over the best individual unchanged
        next_population.push_back(population[gen_best_idx]);

        // Fill the rest
        std::uniform_real_distribution<double> prob(0.0, 1.0);
        while (next_population.size() < config_.population_size) {
            int parent_a = tournament_select(fitness, rng);
            int parent_b = tournament_select(fitness, rng);

            Individual child;
            if (prob(rng) < config_.crossover_rate) {
                child = crossover(population[parent_a], population[parent_b], rng);
            } else {
                child = population[parent_a];
            }
            child = mutate(child, rng);
            next_population.push_back(std::move(child));
        }

        population = std::move(next_population);
    }

    return result;
}
