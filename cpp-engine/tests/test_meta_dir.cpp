#include <gtest/gtest.h>
#include "api/meta_dir.h"
#include <atomic>
#include <filesystem>
#include <fstream>
#include <unistd.h>

namespace fs = std::filesystem;

namespace {
struct TempMeta {
    fs::path dir;
    TempMeta() {
        static std::atomic<unsigned> sequence{0};
        dir = fs::temp_directory_path() / ("longshort_meta_" + std::to_string(getpid()) + "_" + std::to_string(sequence++));
        fs::create_directories(dir);
    }
    ~TempMeta() { std::error_code error; fs::remove_all(dir, error); }
    void file(const std::string& rel, const std::string& body) {
        fs::create_directories((dir / rel).parent_path());
        std::ofstream(dir / rel, std::ios::binary) << body;
    }
};
std::string error_of(const fs::path& dir, const std::string& name) {
    try { meta_dir::members(dir, name); } catch (const std::runtime_error& e) { return e.what(); }
    return "";
}
}  // namespace

TEST(MetaDir, MembersAreRecursiveSortedAndVerbatim) {
    TempMeta meta;
    meta.file("s/sub/c.xml", "<c/>\n");
    meta.file("s/b.xml", "<b/>\r\n  trailing ");
    meta.file("s/a.xml", "<a/>\n");
    meta.file("s/notes.txt", "x");
    meta.file("s/.hidden.xml", "<h/>");
    meta.file("s/.hiddendir/d.xml", "<d/>");
    meta.file("s/sub2/Z.xml", "<z/>");
    const auto members = meta_dir::members(meta.dir, "s");
    std::vector<std::string> paths;
    for (const auto& m : members) paths.push_back(m.path);
    EXPECT_EQ(paths, (std::vector<std::string>{"a.xml", "b.xml", "sub/c.xml", "sub2/Z.xml"}));
    EXPECT_EQ(members[1].xml, "<b/>\r\n  trailing ");
    EXPECT_EQ(members[2].xml, "<c/>\n");
}

TEST(MetaDir, SymlinkedDirectoriesAreEnteredOnce) {
    TempMeta meta;
    meta.file("s/real/a.xml", "<a/>");
    fs::create_directory_symlink(meta.dir / "s" / "real", meta.dir / "s" / "link");
    fs::create_directory_symlink(meta.dir / "s", meta.dir / "s" / "real" / "loop");
    fs::create_directory_symlink(meta.dir / "s" / "real", meta.dir / "s" / "zzz");
    const auto members = meta_dir::members(meta.dir, "s");
    ASSERT_EQ(members.size(), 1u);
    EXPECT_EQ(members[0].path, "link/a.xml");  // first alias by name wins
}

TEST(MetaDir, XmlSymlinkThatIsNotARegularFileThrows) {
    TempMeta meta;
    meta.file("s/a.xml", "<a/>");
    fs::create_symlink("/nonexistent/c.xml", meta.dir / "s" / "c.xml");
    EXPECT_EQ(error_of(meta.dir, "s"), "Cannot read meta member " + (meta.dir / "s" / "c.xml").string());
    TempMeta loop;
    loop.file("s/a.xml", "<a/>");
    fs::create_directory_symlink(loop.dir / "s", loop.dir / "s" / "d.xml");
    EXPECT_EQ(error_of(loop.dir, "s"), "Cannot read meta member " + (loop.dir / "s" / "d.xml").string());
}

TEST(MetaDir, NamesAreSortedSubdirectoriesAndAbsentDirIsEmpty) {
    TempMeta meta;
    meta.file("zeta/a.xml", "");
    meta.file("alpha/a.xml", "");
    fs::create_directories(meta.dir / "empty");
    fs::create_directories(meta.dir / ".hidden");
    meta.file("file.xml", "");
    EXPECT_EQ(meta_dir::names(meta.dir), (std::vector<std::string>{"alpha", "empty", "zeta"}));
    EXPECT_TRUE(meta_dir::names(meta.dir / "missing").empty());
}

TEST(MetaDir, UnknownInvalidAndEmptyNamesThrow) {
    TempMeta meta;
    meta.file("s/a.xml", "<a/>");
    fs::create_directories(meta.dir / "empty" / "sub");
    meta.file("empty/sub/readme.txt", "");
    meta.file("empty/.x.xml", "");
    EXPECT_EQ(error_of(meta.dir, "nope"), "Unknown meta 'nope': no folder " + (meta.dir / "nope").string());
    EXPECT_EQ(error_of(meta.dir / "missing", "s"),
              "Unknown meta 's': no folder " + (meta.dir / "missing" / "s").string());
    for (const std::string name : {"", "..", ".", "s/..", "../s", "s/sub"})
        EXPECT_EQ(error_of(meta.dir / "s", name).rfind("Unknown meta '" + name + "'", 0), 0u) << name;
    EXPECT_EQ(error_of(meta.dir, "empty"), "meta 'empty' has no .xml files");
}
