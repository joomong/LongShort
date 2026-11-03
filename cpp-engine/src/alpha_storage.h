#pragma once

#include <cstdint>
#include <filesystem>
#include <memory>
#include <string>
#include <vector>

// Saved alphas: <alphas_dir>/<name>/{alpha.xml, metadata.yml, results/, positions/}.
// A save is staged in a hidden sibling directory and renamed into place once complete.
namespace alpha_storage {

struct Metadata {
    std::string level;          // xml | results | positions
    std::string sim_start;
    std::string sim_end;
    std::string is_start;
    std::string os_start;
    std::string universe;
    std::string execution_price;
    unsigned threads = 20;
};

struct SavedAlpha {
    std::string name;
    std::string xml;
    Metadata metadata;
};

// 1..200 of letters, digits, '_', '-', '.'; a leading '.' is reserved for staging directories.
bool is_valid_name(const std::string& name);
bool is_valid_level(const std::string& level);

// Creates the directory, proves it is writable and supports RENAME_NOREPLACE, and removes
// staging directories whose lock is free.
void prepare_directory(const std::filesystem::path& dir);

std::string metadata_yaml(const Metadata& metadata);
bool read_metadata(const std::filesystem::path& file, Metadata* out);
bool read_file(const std::filesystem::path& file, std::string* out);

// Directories with alpha.xml and metadata.yml, by name; unreadable metadata names go to `malformed`.
std::vector<SavedAlpha> list(const std::filesystem::path& dir, std::vector<std::string>* malformed = nullptr);
bool exists(const std::filesystem::path& dir, const std::string& name);
// Removes a real saved-alpha directory (not a symlink, with alpha.xml and metadata.yml).
bool remove(const std::filesystem::path& dir, const std::string& name);

// Invariant: the transaction holds flock(LOCK_EX) on staging() from construction until
// commit/discard; prepare_directory removes only staging dirs whose lock it can take, so
// several engines can share one alphas_dir. alphas_dir must be a local filesystem with
// renameat2(RENAME_NOREPLACE) (ext4/xfs/btrfs/tmpfs); sshfs/NFS are rejected at startup.
class SaveTransaction {
public:
    SaveTransaction(std::filesystem::path dir, std::string name);
    ~SaveTransaction();
    SaveTransaction(const SaveTransaction&) = delete;
    SaveTransaction& operator=(const SaveTransaction&) = delete;

    const std::filesystem::path& staging() const { return staging_; }
    std::filesystem::path final_path() const { return dir_ / name_; }
    // Creates parent directories below staging() (never staging() itself); returns staging()/relative.
    // Throws when staging() has vanished.
    std::filesystem::path prepare(const std::filesystem::path& relative);
    // Writes bytes verbatim to prepare(relative).
    void write(const std::filesystem::path& relative, const std::string& bytes);
    // False when <name> already exists; the staging directory is removed either way.
    bool commit();

private:
    void discard();
    std::filesystem::path dir_;
    std::string name_;
    std::filesystem::path staging_;
    bool done_ = false;
    int lock_fd_ = -1;
};

// Collects one row per (bar, symbol) with a finite non-zero weight, ordered by
// date then symbol id, and writes date32/utf8/float64 columns.
class PositionsWriter {
public:
    explicit PositionsWriter(const std::vector<std::string>& symbols);
    ~PositionsWriter();
    void add_bar(int64_t epoch_ms, const std::vector<double>& weights);
    void write(const std::filesystem::path& file);

private:
    struct Columns;
    std::vector<std::string> symbols_;
    std::vector<std::size_t> order_; // symbol indices sorted by id
    std::unique_ptr<Columns> columns_;
};

} // namespace alpha_storage
