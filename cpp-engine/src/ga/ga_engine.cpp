#include "ga_engine.h"
#include <algorithm>
#include <cmath>
#include <limits>
#include <stdexcept>
#include <thread>
#include <unordered_set>

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
    unsigned threads = config_.n_threads;
    if (!threads) threads = std::max(1u, std::thread::hardware_concurrency());
    ParallelEvaluator evaluator(threads, std::move(evaluate));
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
