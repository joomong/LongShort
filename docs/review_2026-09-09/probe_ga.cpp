#include "ga/ga_engine.h"
#include "ga/search_space.h"
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    SearchSpace space;
    space.template_xml = "<data expr=\"close\"/><tsmean days=\"__GENE_0__\"/>";
    SearchSpace::Gene gene;
    gene.description="tsmean.days";
    gene.values={"2","3"};
    space.genes.push_back(gene);
    GAConfig config;
    config.population_size=argc>1 ? static_cast<unsigned>(std::stoul(argv[1])) : 0;
    config.generations=1;
    config.n_threads=1;
    GAEngine engine("<data expr=\"close\"/><tsmean days=\"2|3\"/>",space,config);
    auto result=engine.run([](const std::string&){return 1.0;});
    std::cout << result.best_xml << "\n";
}
