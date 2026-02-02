"""Genetic Algorithm engine for XML alpha parameter search."""
from __future__ import annotations

import hashlib
import math
import random
from dataclasses import dataclass, field
from itertools import product
from typing import Any, Callable, Dict, List, Optional, Tuple

import xml.etree.ElementTree as ET

from framework.exceptions import XmlCompileError


# ---------------------------------------------------------------------------
# Parsing helpers (replicated from xml_alpha to avoid relying on private API)
# ---------------------------------------------------------------------------

def _split_pipe(value: str) -> List[str]:
    """Split attribute value by pipe."""
    raw = (value or "").strip()
    if not raw:
        return [raw]
    raw = raw.replace("\\|", "|")
    if "|" not in raw:
        return [raw]
    parts = [p.strip() for p in raw.split("|")]
    return [p for p in parts if p != ""]


def _parse_range(value: str) -> List[str]:
    """Parse 'start...step...end' or 'start...end' into a list of string ints."""
    raw = (value or "").strip()
    if "..." not in raw:
        return [raw]
    parts = raw.split("...")
    try:
        if len(parts) == 3:
            start, step, end = int(parts[0]), int(parts[1]), int(parts[2])
        elif len(parts) == 2:
            start, end = int(parts[0]), int(parts[1])
            step = 1
        else:
            return [raw]
    except ValueError as e:
        raise XmlCompileError(f"Invalid range expression: {raw!r}") from e

    if step == 0:
        raise XmlCompileError(f"Invalid range expression: {raw!r}")
    if step > 0 and start > end:
        raise XmlCompileError(f"Invalid range expression: {raw!r}")
    if step < 0 and start < end:
        raise XmlCompileError(f"Invalid range expression: {raw!r}")

    return [str(v) for v in range(start, end + (1 if step > 0 else -1), step)]


# ---------------------------------------------------------------------------
# Search Space Extraction
# ---------------------------------------------------------------------------

@dataclass
class GeneSpec:
    """One axis of the search space."""
    elem_index: int
    attr_name: str
    choices: List[str]
    is_choice: bool = False


@dataclass
class SearchSpace:
    """Complete search space extracted from a template XML."""
    genes: List[GeneSpec]
    elements: List[Tuple[str, Dict[str, str]]]

    @property
    def total_combinations(self) -> int:
        n = 1
        for g in self.genes:
            n *= len(g.choices)
        return n

    def size(self) -> int:
        return len(self.genes)


def extract_search_space(xml: str) -> SearchSpace:
    """Parse XML template and extract all axes of variation.

    Raises:
        XmlCompileError: if XML is invalid, has no variable dimensions, or uses <shuffle>.
    """
    if xml is None or not xml.strip():
        raise XmlCompileError("Empty XML")

    src = xml.strip()

    # Parse into element list (same wrapping logic as xml_alpha.compile())
    try:
        root = ET.fromstring(src)
    except ET.ParseError:
        root = None

    if root is not None:
        # Single root element
        elems = [root]
    else:
        wrapped = f"<alpha>{src}</alpha>"
        try:
            alpha_root = ET.fromstring(wrapped)
        except ET.ParseError as e:
            raise XmlCompileError(f"Invalid XML: {e}") from e
        elems = list(alpha_root)

    if not elems:
        raise XmlCompileError("Empty XML")

    genes: List[GeneSpec] = []
    elements: List[Tuple[str, Dict[str, str]]] = []

    for i, elem in enumerate(elems):
        tag = elem.tag

        if tag == "shuffle":
            raise XmlCompileError("GA search does not support <shuffle> blocks")

        if tag == "var":
            # Store var element metadata but skip gene extraction
            elements.append((tag, dict(elem.attrib)))
            continue

        if tag == "choice":
            bind = (elem.attrib.get("bind") or "").strip()
            if bind:
                raise XmlCompileError("GA search does not support <choice bind='same'>")
            # Each child becomes a choice
            child_xmls: List[str] = []
            for child in list(elem):
                child_xml = ET.tostring(child, encoding="unicode")
                child_xmls.append(child_xml)
            if len(child_xmls) > 1:
                genes.append(GeneSpec(
                    elem_index=i,
                    attr_name="__choice__",
                    choices=child_xmls,
                    is_choice=True,
                ))
            # Store as choice element
            elements.append((tag, dict(elem.attrib)))
            continue

        # Normal element: check each attribute for pipes/ranges
        attribs = dict(elem.attrib)
        for attr_name, attr_value in attribs.items():
            pipe_parts = _split_pipe(attr_value)
            expanded: List[str] = []
            for pp in pipe_parts:
                expanded.extend(_parse_range(pp))
            if len(expanded) > 1:
                genes.append(GeneSpec(
                    elem_index=i,
                    attr_name=attr_name,
                    choices=expanded,
                ))
        elements.append((tag, attribs))

    if len(genes) == 0:
        raise XmlCompileError(
            "No variable dimensions in XML template -- nothing to search"
        )

    return SearchSpace(genes=genes, elements=elements)


# ---------------------------------------------------------------------------
# Genome Representation
# ---------------------------------------------------------------------------

Genome = List[int]


def genome_to_xml(genome: Genome, space: SearchSpace) -> str:
    """Convert a genome (list of choice indices) to a concrete XML string."""
    # Build a mutable copy of elements
    result_parts: List[str] = []

    # Group genes by elem_index for fast lookup
    genes_by_elem: Dict[int, List[Tuple[int, GeneSpec]]] = {}
    for gene_idx, gene in enumerate(space.genes):
        genes_by_elem.setdefault(gene.elem_index, []).append((gene_idx, gene))

    for i, (tag, attribs) in enumerate(space.elements):
        elem_genes = genes_by_elem.get(i, [])

        if tag == "var":
            # Vars are not included in output XML (they are template-only)
            continue

        # Check if there is a choice gene for this element
        choice_gene = None
        normal_genes = []
        for gene_idx, gene in elem_genes:
            if gene.is_choice:
                choice_gene = (gene_idx, gene)
            else:
                normal_genes.append((gene_idx, gene))

        if choice_gene is not None:
            gene_idx, gene = choice_gene
            chosen_xml = gene.choices[genome[gene_idx]]
            result_parts.append(chosen_xml)
            continue

        # Normal element: apply gene overrides to attributes
        concrete_attribs = dict(attribs)
        for gene_idx, gene in normal_genes:
            concrete_attribs[gene.attr_name] = gene.choices[genome[gene_idx]]

        elem = ET.Element(tag, {k: str(v) for k, v in concrete_attribs.items()})
        result_parts.append(ET.tostring(elem, encoding="unicode"))

    return "\n".join(result_parts)


def xml_hash(xml: str) -> str:
    """Stable hash of an XML string for dedup caching."""
    return hashlib.md5(xml.encode()).hexdigest()


# ---------------------------------------------------------------------------
# GA Operators
# ---------------------------------------------------------------------------

@dataclass
class Individual:
    """One candidate in the population."""
    genome: Genome
    xml: str = ""
    fitness: float = float('-inf')
    xml_hash: str = ""


def random_individual(space: SearchSpace) -> Individual:
    """Create a random individual within the search space."""
    genome = [random.randint(0, len(g.choices) - 1) for g in space.genes]
    xml = genome_to_xml(genome, space)
    return Individual(genome=genome, xml=xml, xml_hash=xml_hash(xml))


def crossover(
    parent1: Individual, parent2: Individual, space: SearchSpace
) -> Tuple[Individual, Individual]:
    """Single-point crossover. Returns two children."""
    if space.size() <= 1:
        c1 = Individual(genome=list(parent1.genome))
        c2 = Individual(genome=list(parent2.genome))
    else:
        point = random.randint(1, space.size() - 1)
        g1 = parent1.genome[:point] + parent2.genome[point:]
        g2 = parent2.genome[:point] + parent1.genome[point:]
        c1 = Individual(genome=g1)
        c2 = Individual(genome=g2)

    c1.xml = genome_to_xml(c1.genome, space)
    c1.xml_hash = xml_hash(c1.xml)
    c2.xml = genome_to_xml(c2.genome, space)
    c2.xml_hash = xml_hash(c2.xml)
    return c1, c2


def mutate(
    ind: Individual, space: SearchSpace, mutation_rate: float = 0.1
) -> Individual:
    """Per-gene mutation: each gene has mutation_rate probability of random reassignment."""
    new_genome = list(ind.genome)
    mutated = False
    for i, gene in enumerate(space.genes):
        if random.random() < mutation_rate and len(gene.choices) > 1:
            new_val = random.randint(0, len(gene.choices) - 1)
            if new_val != new_genome[i]:
                new_genome[i] = new_val
                mutated = True
    if mutated:
        xml_str = genome_to_xml(new_genome, space)
        return Individual(
            genome=new_genome, xml=xml_str, xml_hash=xml_hash(xml_str)
        )
    return ind


def tournament_select(population: List[Individual], k: int = 3) -> Individual:
    """Tournament selection: pick k random individuals, return the fittest."""
    candidates = random.sample(population, min(k, len(population)))
    return max(candidates, key=lambda ind: ind.fitness)


# ---------------------------------------------------------------------------
# GA Configuration
# ---------------------------------------------------------------------------

@dataclass
class GAConfig:
    """GA hyperparameters."""
    population_size: int = 50
    generations: int = 100
    elite_count: int = 5
    mutation_rate: float = 0.1
    crossover_rate: float = 0.7
    early_stop_generations: int = 5
    top_n: int = 10

    @classmethod
    def from_frontend(cls, ga_population: int, top_n: int = 10) -> "GAConfig":
        return cls(
            population_size=ga_population,
            generations=ga_population * 2,
            top_n=top_n,
        )


# ---------------------------------------------------------------------------
# Evaluation Cache
# ---------------------------------------------------------------------------

@dataclass
class EvalCache:
    """Cache backtest results by XML hash to avoid redundant computation."""
    _cache: Dict[str, float] = field(default_factory=dict)

    def get(self, h: str) -> Optional[float]:
        return self._cache.get(h)

    def put(self, h: str, fitness: float) -> None:
        self._cache[h] = fitness

    def __len__(self) -> int:
        return len(self._cache)

    def all_individuals(self) -> List[Tuple[str, float]]:
        """Return all cached (xml_hash, fitness) pairs."""
        return list(self._cache.items())


# ---------------------------------------------------------------------------
# GA Main Loop
# ---------------------------------------------------------------------------

@dataclass
class GAResult:
    """Result of a GA run."""
    best_individuals: List[Individual]
    generations_run: int
    total_evaluations: int
    search_space_size: int


def run_ga(
    space: SearchSpace,
    config: GAConfig,
    evaluate_batch: Callable[[List[str]], List[float]],
    progress_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
) -> GAResult:
    """Execute the GA loop.

    Args:
        space: search space extracted from XML template.
        config: GA hyperparameters.
        evaluate_batch: function(xml_list) -> list of IS Sharpe values.
        progress_callback: optional callback for progress events.

    Returns:
        GAResult with top-N individuals.
    """
    cache = EvalCache()
    total_evals = 0

    # --- Exhaustive fallback ---
    if space.total_combinations <= config.population_size:
        all_genomes = list(
            product(*(range(len(g.choices)) for g in space.genes))
        )
        population = []
        for g in all_genomes:
            genome = list(g)
            xml_str = genome_to_xml(genome, space)
            population.append(Individual(
                genome=genome,
                xml=xml_str,
                xml_hash=xml_hash(xml_str),
            ))

        xmls = [ind.xml for ind in population]
        fitnesses = evaluate_batch(xmls)
        total_evals = len(xmls)

        if progress_callback:
            progress_callback({
                "type": "progress",
                "generation": 1,
                "totalGenerations": 1,
                "evaluated": len(xmls),
                "populationSize": len(xmls),
            })

        for ind, fit in zip(population, fitnesses):
            f = fit if not (math.isnan(fit) if isinstance(fit, float) else False) else float('-inf')
            ind.fitness = f
            cache.put(ind.xml_hash, f)

        population.sort(key=lambda ind: ind.fitness, reverse=True)
        top = [ind for ind in population if ind.fitness > float('-inf')][:config.top_n]

        if progress_callback:
            progress_callback({
                "type": "generation_end",
                "generation": 1,
                "totalGenerations": 1,
                "topIndividuals": list(top),
            })

        return GAResult(
            best_individuals=top,
            generations_run=1,
            total_evaluations=total_evals,
            search_space_size=space.total_combinations,
        )

    # --- GA loop ---
    population: List[Individual] = []
    seen: set = set()
    for _ in range(config.population_size):
        ind = random_individual(space)
        if ind.xml_hash not in seen:
            seen.add(ind.xml_hash)
            population.append(ind)

    # Fill back to population_size with more random individuals
    attempts = 0
    while len(population) < config.population_size and attempts < config.population_size * 10:
        ind = random_individual(space)
        if ind.xml_hash not in seen:
            seen.add(ind.xml_hash)
            population.append(ind)
        attempts += 1

    best_fitness = float('-inf')
    stagnation_count = 0
    gen = 0

    for gen in range(config.generations):
        # Evaluate unevaluated individuals
        to_eval: List[str] = []
        to_eval_indices: List[int] = []
        for idx, ind in enumerate(population):
            if ind.fitness == float('-inf'):
                cached = cache.get(ind.xml_hash)
                if cached is not None:
                    ind.fitness = cached
                else:
                    to_eval.append(ind.xml)
                    to_eval_indices.append(idx)

        if to_eval:
            fitnesses = evaluate_batch(to_eval)
            total_evals += len(to_eval)
            for i, fit in enumerate(fitnesses):
                f = fit if not (math.isnan(fit) if isinstance(fit, float) else False) else float('-inf')
                idx = to_eval_indices[i]
                population[idx].fitness = f
                cache.put(population[idx].xml_hash, f)

        if progress_callback:
            progress_callback({
                "type": "progress",
                "generation": gen + 1,
                "totalGenerations": config.generations,
                "evaluated": len(to_eval),
                "populationSize": config.population_size,
            })

        # Sort by fitness
        population.sort(key=lambda ind: ind.fitness, reverse=True)

        # Early stop check
        gen_best = population[0].fitness
        if gen_best > best_fitness:
            best_fitness = gen_best
            stagnation_count = 0
        else:
            stagnation_count += 1

        if progress_callback:
            top_n_list = [ind for ind in population if ind.fitness > float('-inf')][:config.top_n]
            progress_callback({
                "type": "generation_end",
                "generation": gen + 1,
                "totalGenerations": config.generations,
                "topIndividuals": list(top_n_list),
            })

        if stagnation_count >= config.early_stop_generations:
            break

        # Build next generation
        next_gen: List[Individual] = []

        # Elitism
        elite = population[:config.elite_count]
        next_gen.extend(elite)

        # Fill remaining
        while len(next_gen) < config.population_size:
            if random.random() < config.crossover_rate:
                p1 = tournament_select(population)
                p2 = tournament_select(population)
                c1, c2 = crossover(p1, p2, space)
                c1 = mutate(c1, space, config.mutation_rate)
                c2 = mutate(c2, space, config.mutation_rate)
                next_gen.append(c1)
                if len(next_gen) < config.population_size:
                    next_gen.append(c2)
            else:
                p = tournament_select(population)
                c = mutate(
                    Individual(
                        genome=list(p.genome),
                        xml=p.xml,
                        xml_hash=p.xml_hash,
                    ),
                    space,
                    config.mutation_rate,
                )
                next_gen.append(c)

        # Assign cached fitness to children that match known XMLs
        for ind in next_gen:
            if ind.fitness == float('-inf'):
                cached = cache.get(ind.xml_hash)
                if cached is not None:
                    ind.fitness = cached

        population = next_gen[:config.population_size]

    # Collect best from final population
    population.sort(key=lambda ind: ind.fitness, reverse=True)
    all_seen: Dict[str, Individual] = {}
    for ind in population:
        if ind.xml_hash not in all_seen or ind.fitness > all_seen[ind.xml_hash].fitness:
            all_seen[ind.xml_hash] = ind

    all_individuals = sorted(
        all_seen.values(), key=lambda ind: ind.fitness, reverse=True
    )
    top_n = [ind for ind in all_individuals if ind.fitness > float('-inf')][:config.top_n]

    return GAResult(
        best_individuals=top_n,
        generations_run=gen + 1,
        total_evaluations=total_evals,
        search_space_size=space.total_combinations,
    )
