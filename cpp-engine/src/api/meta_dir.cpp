#include "api/meta_dir.h"

#include <algorithm>
#include <fstream>
#include <functional>
#include <set>
#include <sstream>
#include <stdexcept>

namespace fs = std::filesystem;

namespace meta_dir {

std::vector<std::string> names(const fs::path& meta_dir)
{
    std::vector<std::string> out;
    std::error_code error;
    if (!fs::is_directory(meta_dir, error)) return out;
    for (fs::directory_iterator it(meta_dir, error), end; !error && it != end; it.increment(error)) {
        const std::string name = it->path().filename().string();
        std::error_code type_error;
        if (name.empty() || name[0] == '.' || !it->is_directory(type_error)) continue;
        out.push_back(name);
    }
    std::sort(out.begin(), out.end());
    return out;
}

std::vector<MetaMember> members(const fs::path& meta_dir, const std::string& name)
{
    const fs::path root = meta_dir / name;
    std::error_code error;
    if (name.empty() || name.find('/') != std::string::npos || name == "." || name == ".." ||
        !fs::is_directory(root, error))
        throw std::runtime_error("Unknown meta '" + name + "': no folder " + root.string());

    std::vector<MetaMember> out;
    std::set<fs::path> visited;  // canonical directories already entered
    std::function<void(const fs::path&)> walk = [&](const fs::path& dir) {
        std::error_code canonical_error;
        const fs::path canonical = fs::canonical(dir, canonical_error);
        if (canonical_error || !visited.insert(canonical).second) return;
        std::vector<fs::path> entries;
        for (fs::directory_iterator it(dir), end; it != end; ++it) entries.push_back(it->path());
        // Sorted so that of two aliases of one directory the first by name is the one entered.
        std::sort(entries.begin(), entries.end(),
                  [](const fs::path& a, const fs::path& b) { return a.string() < b.string(); });
        for (const auto& entry : entries) {
            const std::string file = entry.filename().string();
            if (file.empty() || file[0] == '.') continue;
            if (entry.extension() == ".xml" && fs::is_symlink(entry) && !fs::is_regular_file(entry))
                throw std::runtime_error("Cannot read meta member " + entry.string());
            if (fs::is_directory(entry)) {
                walk(entry);
            } else if (fs::is_regular_file(entry) && entry.extension() == ".xml") {
                std::ifstream in(entry, std::ios::binary);
                if (!in) throw std::runtime_error("Cannot read meta member " + entry.string());
                std::ostringstream text;
                text << in.rdbuf();
                out.push_back({entry.lexically_relative(root).generic_string(), text.str()});
            }
        }
    };
    walk(root);
    if (out.empty()) throw std::runtime_error("meta '" + name + "' has no .xml files");
    std::sort(out.begin(), out.end(),
              [](const MetaMember& a, const MetaMember& b) { return a.path < b.path; });
    return out;
}

}  // namespace meta_dir
