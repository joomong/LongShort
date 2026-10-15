#pragma once
#include <string>
#include <vector>
#include <functional>
#include <random>
#include "search_space.h"
#include "parallel_evaluator.h"

struct GAConfig {
    unsigned population_size  = 50;
    unsigned generations      = 40;
    unsigned first_search     = 200;
    unsigned stop_alphas      = 400;
    double   random_fraction  = 0.2;
    unsigned tournament_size  = 3;
    double   crossover_rate   = 0.8;
    double   mutation_rate    = 0.1;
    unsigned n_threads        = 20;
    unsigned seed             = 42;
};

struct GAResult {
    std::string best_xml;
    // per-generation record: (best_xml, best_fitness)
    std::vector<std::pair<std::string, double>> history;
    size_t evaluated = 0;
    std::string stop_reason;
};

struct GACandidate {
    size_t id = 0;
    unsigned round = 0;
    std::string xml;
    std::string origin;
    double fitness = 0.0;
    std::string error;
};

struct GAEvents {
    std::function<void(unsigned, size_t)> round_start;
    std::function<void(const GACandidate&)> candidate;
    std::function<void(unsigned, const std::string&, double)> round_end;
    // Called with every round's candidate XMLs (the initial search included) before they are evaluated.
    std::function<void(const std::vector<std::string>& xmls)> round_prepare;
    ParallelEvaluator::Cancelled cancelled;
};

class GAEngine {
public:
    GAEngine(const std::string& xml_template,
             const SearchSpace& space,
             const GAConfig& config);

    // The caller supplies the objective; GA maximizes the returned scalar.
    using EvalFunc = std::function<double(const std::string& xml)>;
    using ProgressCallback = std::function<void(unsigned gen,
                                                const std::string& xml,
                                                double fitness)>;

    // Run GA, return best result.
    GAResult run(EvalFunc evaluate);

    // Run GA with per-generation progress callback.
    GAResult run_streaming(EvalFunc evaluate, ProgressCallback cb);
    GAResult run_detailed(EvalFunc evaluate, const GAEvents& events);

private:
    std::string  xml_template_;
    SearchSpace  space_;
    GAConfig     config_;

    using Individual = std::vector<int>;

    Individual random_individual(std::mt19937& rng) const;
    Individual crossover(const Individual& a,
                         const Individual& b,
                         std::mt19937& rng) const;
    Individual mutate(const Individual& ind, std::mt19937& rng) const;
    int        tournament_select(const std::vector<double>& fitness,
                                 std::mt19937& rng) const;

    // Core loop shared by run() and run_streaming()
    GAResult run_impl(EvalFunc evaluate, const GAEvents& events);
};
