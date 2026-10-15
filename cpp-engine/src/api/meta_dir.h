#pragma once

#include <filesystem>
#include <string>
#include <vector>

// Meta member sets: <meta_dir>/<name>/**/*.xml, one alpha XML per file.
namespace meta_dir {

struct MetaMember {
    std::string path;  // relative to <meta_dir>/<name>
    std::string xml;   // file content verbatim
};

// Immediate subdirectories of meta_dir (dot entries skipped), sorted; {} if meta_dir is absent.
std::vector<std::string> names(const std::filesystem::path& meta_dir);

// All *.xml files under <meta_dir>/<name>, recursively, sorted by relative path. Dot entries are
// skipped; symlinks are followed but a directory is entered at most once. Throws std::runtime_error
// for an unknown/invalid name or a folder without .xml files.
std::vector<MetaMember> members(const std::filesystem::path& meta_dir, const std::string& name);

}  // namespace meta_dir
