#include "alpha_storage.h"

#include <algorithm>
#include <atomic>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstring>
#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <fstream>
#include <sstream>
#include <stdexcept>
#include <unistd.h>

#include <arrow/api.h>
#include <arrow/io/file.h>
#include <parquet/arrow/writer.h>
#include <yaml-cpp/yaml.h>

namespace fs = std::filesystem;

namespace alpha_storage {

namespace {

const char* const kMetadataKeys[] = {"level", "simStart", "simEnd", "isStart", "osStart", "universe",
                                     "executionPrice"};

std::string* metadata_field(Metadata& metadata, const std::string& key) {
    if (key == "level") return &metadata.level;
    if (key == "simStart") return &metadata.sim_start;
    if (key == "simEnd") return &metadata.sim_end;
    if (key == "isStart") return &metadata.is_start;
    if (key == "osStart") return &metadata.os_start;
    if (key == "universe") return &metadata.universe;
    if (key == "executionPrice") return &metadata.execution_price;
    return nullptr;
}

std::string unique_suffix() {
    static std::atomic<unsigned long> sequence{0};
    const auto now = std::chrono::steady_clock::now().time_since_epoch().count();
    return std::to_string(::getpid()) + "-" + std::to_string(sequence++) + "-" + std::to_string(now);
}

void check(const arrow::Status& status, const char* what) {
    if (!status.ok()) throw std::runtime_error(std::string(what) + ": " + status.ToString());
}

} // namespace

bool is_valid_name(const std::string& name) {
    if (name.empty() || name.size() > 200 || name.front() == '.') return false;
    for (unsigned char ch : name)
        if (!std::isalnum(ch) && ch != '_' && ch != '-' && ch != '.') return false;
    return true;
}

bool is_valid_level(const std::string& level) {
    return level == "xml" || level == "results" || level == "positions";
}

void prepare_directory(const fs::path& dir) {
    std::error_code error;
    fs::create_directories(dir, error);
    if (error || !fs::is_directory(dir))
        throw std::runtime_error("Cannot create alphas_dir " + dir.string() +
                                 (error ? ": " + error.message() : ""));
    const fs::path probe = dir / (".probe-" + unique_suffix());
    {
        std::ofstream out(probe, std::ios::binary);
        out << "probe";
        out.close();
        if (!out) {
            fs::remove(probe, error);
            throw std::runtime_error("alphas_dir is not writable: " + dir.string());
        }
    }
    // Saves are published with RENAME_NOREPLACE; refuse filesystems that lack it.
    const fs::path renamed = probe.string() + ".r";
    const int renamed_ok = ::renameat2(AT_FDCWD, probe.c_str(), AT_FDCWD, renamed.c_str(), RENAME_NOREPLACE);
    const int rename_error = errno;
    fs::remove(probe, error);
    fs::remove(renamed, error);
    if (renamed_ok != 0) {
        if (rename_error == EINVAL || rename_error == ENOSYS || rename_error == EOPNOTSUPP)
            throw std::runtime_error("alphas_dir filesystem does not support renameat2(RENAME_NOREPLACE) "
                                     "(sshfs/NFS?): saves cannot be published: " + dir.string());
        throw std::runtime_error("alphas_dir rename probe failed: " + dir.string() + ": " + std::strerror(rename_error));
    }
    // Staging directories .<name>.tmp-* whose save no longer holds the lock.
    for (const auto& entry : fs::directory_iterator(dir, error)) {
        const std::string name = entry.path().filename().string();
        const auto tmp = name.find(".tmp-", 1);
        if (name.size() < 2 || name.front() != '.' || tmp == std::string::npos ||
            !is_valid_name(name.substr(1, tmp - 1)))
            continue;
        const int fd = ::open(entry.path().c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (fd < 0) continue;
        if (::flock(fd, LOCK_EX | LOCK_NB) == 0) {
            std::error_code ignored;
            fs::remove_all(entry.path(), ignored);
        }
        ::close(fd);
    }
}

std::string metadata_yaml(const Metadata& metadata) {
    Metadata copy = metadata;
    YAML::Emitter out;
    out << YAML::BeginMap;
    for (const char* key : kMetadataKeys)
        out << YAML::Key << key << YAML::Value << *metadata_field(copy, key);
    out << YAML::Key << "threads" << YAML::Value << metadata.threads;
    out << YAML::EndMap;
    return std::string(out.c_str()) + "\n";
}

bool read_metadata(const fs::path& file, Metadata* out) {
    try {
        const YAML::Node root = YAML::LoadFile(file.string());
        if (!root.IsMap()) return false;
        Metadata metadata;
        for (const char* key : kMetadataKeys) {
            if (!root[key] || !root[key].IsScalar()) return false;
            *metadata_field(metadata, key) = root[key].as<std::string>();
        }
        metadata.threads = root["threads"].as<unsigned>(metadata.threads);
        if (!is_valid_level(metadata.level)) return false;
        *out = std::move(metadata);
        return true;
    } catch (const std::exception&) {
        return false;
    }
}

bool read_file(const fs::path& file, std::string* out) {
    std::ifstream in(file, std::ios::binary);
    if (!in) return false;
    std::ostringstream buffer;
    buffer << in.rdbuf();
    *out = buffer.str();
    return true;
}

std::vector<SavedAlpha> list(const fs::path& dir, std::vector<std::string>* malformed) {
    std::vector<SavedAlpha> alphas;
    std::error_code error;
    for (const auto& entry : fs::directory_iterator(dir, error)) {
        const std::string name = entry.path().filename().string();
        if (!is_valid_name(name) || !entry.is_directory(error)) continue;
        SavedAlpha alpha;
        alpha.name = name;
        if (!read_file(entry.path() / "alpha.xml", &alpha.xml) || !fs::exists(entry.path() / "metadata.yml", error))
            continue;
        if (!read_metadata(entry.path() / "metadata.yml", &alpha.metadata)) {
            if (malformed) malformed->push_back(name);
            continue;
        }
        alphas.push_back(std::move(alpha));
    }
    std::sort(alphas.begin(), alphas.end(), [](const auto& a, const auto& b) { return a.name < b.name; });
    return alphas;
}

bool exists(const fs::path& dir, const std::string& name) {
    std::error_code error;
    return fs::exists(fs::symlink_status(dir / name, error));
}

bool remove(const fs::path& dir, const std::string& name) {
    if (!is_valid_name(name)) return false;
    std::error_code error;
    const fs::path target = dir / name;
    const auto status = fs::symlink_status(target, error);
    if (error || !fs::is_directory(status)) return false;  // symlinks and files are never followed
    if (!fs::is_regular_file(fs::symlink_status(target / "alpha.xml", error)) ||
        !fs::is_regular_file(fs::symlink_status(target / "metadata.yml", error)))
        return false;
    fs::remove_all(dir / name);
    return true;
}

SaveTransaction::SaveTransaction(fs::path dir, std::string name)
    : dir_(std::move(dir)), name_(std::move(name)) {
    if (!is_valid_name(name_)) throw std::invalid_argument("Invalid alpha name");
    staging_ = dir_ / ("." + name_ + ".tmp-" + unique_suffix());
    if (!fs::create_directory(staging_))
        throw std::runtime_error("Cannot create staging directory " + staging_.string());
    auto fail = [&](const std::string& what) {
        const int error = errno;
        if (lock_fd_ >= 0) ::close(lock_fd_);
        lock_fd_ = -1;
        throw std::runtime_error("Cannot lock staging directory " + staging_.string() + ": " + what +
                                 (error ? std::string(": ") + std::strerror(error) : ""));
    };
    lock_fd_ = ::open(staging_.c_str(), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (lock_fd_ < 0) fail("open");
    int locked;
    do locked = ::flock(lock_fd_, LOCK_EX | LOCK_NB);
    while (locked != 0 && errno == EINTR);
    if (locked != 0) fail("flock");
    struct stat opened {}, named {};
    if (::fstat(lock_fd_, &opened) != 0 || ::lstat(staging_.c_str(), &named) != 0) fail("stat");
    if (opened.st_dev != named.st_dev || opened.st_ino != named.st_ino) {
        errno = 0;
        fail("directory was replaced");
    }
}

SaveTransaction::~SaveTransaction() { discard(); }

void SaveTransaction::discard() {
    if (done_) return;
    done_ = true;
    std::error_code error;
    fs::remove_all(staging_, error);  // still locked, so no sweep races the removal
    if (lock_fd_ >= 0) ::close(lock_fd_);
    lock_fd_ = -1;
}

fs::path SaveTransaction::prepare(const fs::path& relative) {
    if (done_) throw std::logic_error("Save transaction already finished");
    fs::path current = staging_;
    for (const auto& part : relative.parent_path()) {
        current /= part;
        std::error_code error;
        fs::create_directory(current, error);  // never creates staging_ itself
        if (error) throw std::runtime_error("Cannot create " + current.string() + ": " + error.message());
        if (!fs::is_directory(fs::symlink_status(current, error)))
            throw std::runtime_error("Cannot create " + current.string());
    }
    if (!fs::is_directory(fs::symlink_status(staging_)))
        throw std::runtime_error("Staging directory vanished: " + staging_.string());
    return staging_ / relative;
}

void SaveTransaction::write(const fs::path& relative, const std::string& bytes) {
    const fs::path file = prepare(relative);
    std::ofstream out(file, std::ios::binary);
    out.write(bytes.data(), static_cast<std::streamsize>(bytes.size()));
    out.close();
    if (!out) throw std::runtime_error("Cannot write " + file.string());
}

bool SaveTransaction::commit() {
    if (done_) throw std::logic_error("Save transaction already finished");
    // RENAME_NOREPLACE: a concurrent save of the same name cannot be replaced.
    if (::renameat2(AT_FDCWD, staging_.c_str(), AT_FDCWD, final_path().c_str(), RENAME_NOREPLACE) == 0) {
        done_ = true;
        ::close(lock_fd_);
        lock_fd_ = -1;
        return true;
    }
    const int error = errno;
    discard();
    if (error == EEXIST || error == ENOTEMPTY) return false;
    throw std::runtime_error("Cannot publish " + final_path().string() + ": " + std::strerror(error));
}

struct PositionsWriter::Columns {
    arrow::Date32Builder date;
    arrow::StringBuilder symbol;
    arrow::DoubleBuilder weight;
};

PositionsWriter::PositionsWriter(const std::vector<std::string>& symbols)
    : symbols_(symbols), order_(symbols.size()), columns_(std::make_unique<Columns>()) {
    for (std::size_t i = 0; i < order_.size(); ++i) order_[i] = i;
    std::sort(order_.begin(), order_.end(), [&](std::size_t a, std::size_t b) { return symbols_[a] < symbols_[b]; });
}

PositionsWriter::~PositionsWriter() = default;

void PositionsWriter::add_bar(int64_t epoch_ms, const std::vector<double>& weights) {
    const int64_t day_ms = 86400000;
    const auto day = static_cast<int32_t>(epoch_ms >= 0 ? epoch_ms / day_ms : -((-epoch_ms + day_ms - 1) / day_ms));
    for (std::size_t i : order_) {
        const double w = weights[i];
        if (!std::isfinite(w) || w == 0.0) continue;
        check(columns_->date.Append(day), "positions date");
        check(columns_->symbol.Append(symbols_[i]), "positions symbol");
        check(columns_->weight.Append(w), "positions weight");
    }
}

void PositionsWriter::write(const fs::path& file) {
    std::shared_ptr<arrow::Array> date, symbol, weight;
    check(columns_->date.Finish(&date), "positions date");
    check(columns_->symbol.Finish(&symbol), "positions symbol");
    check(columns_->weight.Finish(&weight), "positions weight");
    auto schema = arrow::schema({arrow::field("date", arrow::date32(), false),
                                 arrow::field("tradingItemId", arrow::utf8(), false),
                                 arrow::field("weight", arrow::float64(), false)});
    auto table = arrow::Table::Make(schema, {date, symbol, weight});
    auto output = arrow::io::FileOutputStream::Open(file.string());
    check(output.status(), "positions file");
    check(parquet::arrow::WriteTable(*table, arrow::default_memory_pool(), *output, 1 << 20), "positions parquet");
    check((*output)->Close(), "positions file");
}

} // namespace alpha_storage
