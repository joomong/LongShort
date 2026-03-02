"""Tests for the GA engine (search space, operators, main loop)."""
import math
import random

import pytest

from api.services.ga_engine import (
    EvalCache,
    GAConfig,
    GAResult,
    GeneSpec,
    Individual,
    SearchSpace,
    crossover,
    extract_search_space,
    genome_to_xml,
    mutate,
    random_individual,
    run_ga,
    tournament_select,
    xml_hash,
)
from framework.exceptions import XmlCompileError


# =========================================================================
# TestSearchSpaceExtraction
# =========================================================================


class TestSearchSpaceExtraction:
    def test_single_pipe_attribute(self):
        """<data expr="close|open" delay="1"/> -> 1 gene with 2 choices."""
        xml = '<data expr="close|open" delay="1"/>'
        space = extract_search_space(xml)
        assert len(space.genes) == 1
        gene = space.genes[0]
        assert gene.attr_name == "expr"
        assert gene.choices == ["close", "open"]

    def test_range_attribute(self):
        """<data expr="close" delay="1"/><tsmean days="5...5...20"/> -> 1 gene, 4 choices."""
        xml = '<data expr="close" delay="1"/><tsmean days="5...5...20"/>'
        space = extract_search_space(xml)
        assert len(space.genes) == 1
        gene = space.genes[0]
        assert gene.attr_name == "days"
        assert gene.choices == ["5", "10", "15", "20"]

    def test_mixed_pipe_and_range(self):
        """Template with both pipe and range across multiple elements."""
        xml = '<data expr="close|open" delay="1"/><tsmean days="5...5...20"/>'
        space = extract_search_space(xml)
        assert len(space.genes) == 2
        # First gene: expr pipe
        assert space.genes[0].choices == ["close", "open"]
        # Second gene: days range
        assert space.genes[1].choices == ["5", "10", "15", "20"]

    def test_no_variable_dimensions_raises(self):
        """Fixed XML with no pipes/ranges raises XmlCompileError."""
        xml = '<data expr="close" delay="1"/>'
        with pytest.raises(XmlCompileError, match="No variable dimensions"):
            extract_search_space(xml)

    def test_shuffle_raises(self):
        """<shuffle> in template raises XmlCompileError."""
        xml = '<shuffle><data expr="close" delay="1"/></shuffle>'
        with pytest.raises(XmlCompileError, match="shuffle"):
            extract_search_space(xml)

    def test_total_combinations(self):
        """total_combinations matches product of all gene choice counts."""
        xml = '<data expr="close|open" delay="1"/><tsmean days="5...5...20"/>'
        space = extract_search_space(xml)
        # 2 choices * 4 choices = 8
        assert space.total_combinations == 8

    def test_choice_element(self):
        """<choice> block creates a choice gene with child XML strings."""
        xml = '<data expr="close" delay="1"/><choice><rank /><zscore /></choice>'
        space = extract_search_space(xml)
        assert len(space.genes) == 1
        gene = space.genes[0]
        assert gene.is_choice is True
        assert gene.attr_name == "__choice__"
        assert len(gene.choices) == 2
        assert "rank" in gene.choices[0]
        assert "zscore" in gene.choices[1]

    def test_choice_bind_raises(self):
        """<choice bind='same'> raises XmlCompileError in GA mode."""
        xml = '<data expr="close" delay="1"/><choice bind="same"><rank /><zscore /></choice>'
        with pytest.raises(XmlCompileError, match="choice bind"):
            extract_search_space(xml)

    def test_empty_xml_raises(self):
        """Empty or whitespace-only XML raises XmlCompileError."""
        with pytest.raises(XmlCompileError, match="Empty XML"):
            extract_search_space("")
        with pytest.raises(XmlCompileError, match="Empty XML"):
            extract_search_space("   ")

    def test_multi_element_pipeline(self):
        """Multi-element pipeline extracts genes from all elements."""
        xml = '<data expr="close|open" delay="1|2"/><tsmean days="5...5...20"/><rank />'
        space = extract_search_space(xml)
        # 3 genes: expr (2), delay (2), days (4)
        assert len(space.genes) == 3
        assert space.total_combinations == 2 * 2 * 4


# =========================================================================
# TestGenomeToXml
# =========================================================================


class TestGenomeToXml:
    def test_basic_conversion(self):
        """Genome [0, 2] maps to correct concrete attribute values."""
        xml = '<data expr="close|open" delay="1"/><tsmean days="5...5...20"/>'
        space = extract_search_space(xml)
        result = genome_to_xml([0, 2], space)
        assert 'expr="close"' in result
        assert 'days="15"' in result

    def test_roundtrip_compiles(self):
        """Generated XML can be parsed by ElementTree (valid XML)."""
        import xml.etree.ElementTree as ET

        xml = '<data expr="close|open" delay="1"/><tsmean days="5...5...20"/>'
        space = extract_search_space(xml)
        result = genome_to_xml([1, 0], space)
        # Wrap and parse to verify it is valid XML
        wrapped = f"<alpha>{result}</alpha>"
        root = ET.fromstring(wrapped)
        children = list(root)
        assert len(children) == 2

    def test_genome_mapping_second_choice(self):
        """Genome [1, 3] selects second expr and fourth days."""
        xml = '<data expr="close|open" delay="1"/><tsmean days="5...5...20"/>'
        space = extract_search_space(xml)
        result = genome_to_xml([1, 3], space)
        assert 'expr="open"' in result
        assert 'days="20"' in result

    def test_choice_genome_selects_child(self):
        """Choice gene [0] selects the first child element."""
        xml = '<data expr="close" delay="1"/><choice><rank /><zscore /></choice>'
        space = extract_search_space(xml)
        result0 = genome_to_xml([0], space)
        assert "rank" in result0
        assert "zscore" not in result0

        result1 = genome_to_xml([1], space)
        assert "zscore" in result1
        assert "rank" not in result1


# =========================================================================
# TestGAOperators
# =========================================================================


class TestGAOperators:
    @pytest.fixture
    def space_and_parents(self):
        """Create a search space and two parent individuals."""
        xml = '<data expr="close|open|volume" delay="1"/><tsmean days="5...5...20"/>'
        space = extract_search_space(xml)
        p1 = Individual(genome=[0, 0])
        p1.xml = genome_to_xml(p1.genome, space)
        p1.xml_hash = xml_hash(p1.xml)
        p1.fitness = 1.0

        p2 = Individual(genome=[2, 3])
        p2.xml = genome_to_xml(p2.genome, space)
        p2.xml_hash = xml_hash(p2.xml)
        p2.fitness = 0.5

        return space, p1, p2

    def test_crossover_produces_valid_offspring(self, space_and_parents):
        """Children have gene values within bounds of search space."""
        space, p1, p2 = space_and_parents
        random.seed(42)
        c1, c2 = crossover(p1, p2, space)
        for child in [c1, c2]:
            for i, gene in enumerate(space.genes):
                assert 0 <= child.genome[i] < len(gene.choices)

    def test_crossover_single_gene(self):
        """Single-gene space: crossover returns clones."""
        xml = '<data expr="close|open" delay="1"/>'
        space = extract_search_space(xml)
        p1 = Individual(genome=[0])
        p1.xml = genome_to_xml(p1.genome, space)
        p1.xml_hash = xml_hash(p1.xml)

        p2 = Individual(genome=[1])
        p2.xml = genome_to_xml(p2.genome, space)
        p2.xml_hash = xml_hash(p2.xml)

        c1, c2 = crossover(p1, p2, space)
        # With single gene, children are clones of parents
        assert c1.genome == [0]
        assert c2.genome == [1]

    def test_mutation_stays_in_bounds(self):
        """All mutated gene values are valid indices."""
        xml = '<data expr="close|open|volume" delay="1"/><tsmean days="5...5...20"/>'
        space = extract_search_space(xml)
        ind = Individual(genome=[0, 0])
        ind.xml = genome_to_xml(ind.genome, space)
        ind.xml_hash = xml_hash(ind.xml)

        random.seed(0)
        for _ in range(100):
            mutated = mutate(ind, space, mutation_rate=0.9)
            for i, gene in enumerate(space.genes):
                assert 0 <= mutated.genome[i] < len(gene.choices)

    def test_mutation_single_choice_gene_unchanged(self):
        """Genes with only 1 choice are never mutated."""
        # Build a space where one gene has only 1 choice
        genes = [
            GeneSpec(elem_index=0, attr_name="expr", choices=["close"]),
            GeneSpec(elem_index=1, attr_name="days", choices=["5", "10", "15"]),
        ]
        elements = [("data", {"expr": "close", "delay": "1"}), ("tsmean", {"days": "5|10|15"})]
        space = SearchSpace(genes=genes, elements=elements)

        ind = Individual(genome=[0, 1])
        ind.xml = genome_to_xml(ind.genome, space)
        ind.xml_hash = xml_hash(ind.xml)

        random.seed(0)
        for _ in range(100):
            mutated = mutate(ind, space, mutation_rate=1.0)
            # The single-choice gene should never change
            assert mutated.genome[0] == 0

    def test_tournament_selection_returns_fittest(self):
        """Tournament with k=population_size returns the individual with highest fitness."""
        population = [
            Individual(genome=[0], fitness=1.0),
            Individual(genome=[1], fitness=5.0),
            Individual(genome=[0], fitness=3.0),
        ]
        # With k >= len(population), the fittest should always win
        winner = tournament_select(population, k=3)
        assert winner.fitness == 5.0


# =========================================================================
# TestEvalCache
# =========================================================================


class TestEvalCache:
    def test_put_and_get(self):
        """Cache stores and retrieves fitness by hash."""
        cache = EvalCache()
        cache.put("abc123", 1.5)
        assert cache.get("abc123") == 1.5

    def test_miss_returns_none(self):
        """Cache returns None for unknown hash."""
        cache = EvalCache()
        assert cache.get("unknown") is None


# =========================================================================
# TestGALoop
# =========================================================================


class TestGALoop:
    """Tests for the run_ga main loop."""

    @staticmethod
    def _make_space():
        """Create a small search space: 2 genes, 3 and 4 choices."""
        xml = '<data expr="close|open|volume" delay="1"/><tsmean days="5...5...20"/>'
        return extract_search_space(xml)

    def test_converges_to_known_optimum(self):
        """Mock evaluate_batch rewards genome [1,3]; GA should find it."""
        space = self._make_space()
        target = [1, 3]  # expr="open", days="20"

        def mock_evaluate(xml_list):
            results = []
            for xml_str in xml_list:
                # Compute fitness as negative distance from target
                fitness = 0.0
                if 'expr="open"' in xml_str:
                    fitness += 5.0
                if 'days="20"' in xml_str:
                    fitness += 5.0
                results.append(fitness)
            return results

        config = GAConfig(
            population_size=20,
            generations=50,
            elite_count=3,
            mutation_rate=0.2,
            crossover_rate=0.7,
            early_stop_generations=10,
            top_n=5,
        )
        random.seed(42)
        result = run_ga(space, config, mock_evaluate)

        assert len(result.best_individuals) > 0
        best = result.best_individuals[0]
        assert best.fitness == 10.0
        assert 'expr="open"' in best.xml
        assert 'days="20"' in best.xml

    def test_early_stop(self):
        """GA stops before max generations when fitness plateaus."""
        space = self._make_space()

        def constant_fitness(xml_list):
            return [1.0] * len(xml_list)

        config = GAConfig(
            population_size=10,
            generations=100,
            elite_count=2,
            mutation_rate=0.1,
            crossover_rate=0.7,
            early_stop_generations=3,
            top_n=5,
        )
        random.seed(42)
        result = run_ga(space, config, constant_fitness)
        # Should stop well before 100 generations due to early stopping
        # First gen finds best=1.0, then stagnates for 3 gens -> stops around gen 4
        assert result.generations_run < 100

    def test_handles_all_nan_fitness(self):
        """All-NaN evaluate_batch -> no crash, empty or -inf best."""
        space = self._make_space()

        def all_nan(xml_list):
            return [float("nan")] * len(xml_list)

        config = GAConfig(
            population_size=10,
            generations=5,
            elite_count=2,
            mutation_rate=0.1,
            crossover_rate=0.7,
            early_stop_generations=3,
            top_n=5,
        )
        random.seed(42)
        result = run_ga(space, config, all_nan)
        # Should not crash; best_individuals may be empty (all -inf filtered out)
        assert isinstance(result, GAResult)
        assert len(result.best_individuals) == 0

    def test_exhaustive_fallback(self):
        """Small space uses brute force, generations_run == 1."""
        # 2 choices x 2 choices = 4 combinations, pop_size=10 -> exhaustive
        xml = '<data expr="close|open" delay="1|2"/>'
        space = extract_search_space(xml)
        assert space.total_combinations == 4

        call_count = [0]

        def counting_evaluate(xml_list):
            call_count[0] += 1
            return [1.0 + i * 0.1 for i in range(len(xml_list))]

        config = GAConfig(
            population_size=10,
            generations=50,
            top_n=5,
        )
        result = run_ga(space, config, counting_evaluate)
        assert result.generations_run == 1
        assert result.total_evaluations == 4
        assert call_count[0] == 1  # Single batch call

    def test_returns_top_n(self):
        """GAResult.best_individuals has at most config.top_n entries."""
        space = self._make_space()

        def varied_fitness(xml_list):
            return [random.random() for _ in xml_list]

        config = GAConfig(
            population_size=15,
            generations=10,
            elite_count=2,
            mutation_rate=0.2,
            crossover_rate=0.7,
            early_stop_generations=5,
            top_n=3,
        )
        random.seed(42)
        result = run_ga(space, config, varied_fitness)
        assert len(result.best_individuals) <= 3

    def test_eval_cache_dedup(self):
        """evaluate_batch called fewer times than total individuals across generations."""
        space = self._make_space()
        total_xmls_evaluated = [0]

        def counting_evaluate(xml_list):
            total_xmls_evaluated[0] += len(xml_list)
            return [1.0] * len(xml_list)

        config = GAConfig(
            population_size=10,
            generations=20,
            elite_count=2,
            mutation_rate=0.1,
            crossover_rate=0.7,
            early_stop_generations=3,
            top_n=5,
        )
        random.seed(42)
        result = run_ga(space, config, counting_evaluate)

        # Total evaluations should be less than pop_size * generations_run
        # because the cache deduplicates repeated XMLs
        max_possible = config.population_size * result.generations_run
        assert result.total_evaluations < max_possible
