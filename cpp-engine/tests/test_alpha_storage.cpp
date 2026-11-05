#include <gtest/gtest.h>
#include "alpha_storage.h"
#include "xml_compiler.h"

#include <arrow/api.h>
#include <arrow/io/file.h>
#include <parquet/arrow/reader.h>
#include <fcntl.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

#include <atomic>
#include <filesystem>
#include <fstream>
#include <limits>
#include <string>

namespace fs = std::filesystem;

namespace {
struct TempDir {
    fs::path dir;
    TempDir() {
        static std::atomic<unsigned> sequence{0};
        dir = fs::temp_directory_path() / ("longshort_alpha_storage_" + std::to_string(getpid()) + "_" +
                                           std::to_string(sequence++));
        fs::create_directories(dir);
    }
    ~TempDir() {
        std::error_code error;
        fs::permissions(dir, fs::perms::owner_all, fs::perm_options::add, error);
        fs::remove_all(dir, error);
    }
};

alpha_storage::Metadata sample_metadata() {
    alpha_storage::Metadata metadata;
    metadata.level = "xml";
    metadata.sim_start = "2016-01-04";
    metadata.sim_end = "2026-09-15";
    metadata.is_start = "2016-01-04";
    metadata.os_start = "2024-01-01";
    metadata.universe = "TOP3000";
    metadata.execution_price = "vwap";
    return metadata;
}

std::size_t entry_count(const fs::path& dir) {
    return static_cast<std::size_t>(std::distance(fs::directory_iterator(dir), fs::directory_iterator()));
}
} // namespace

TEST(AlphaStorage, NamesRejectTraversalAndHiddenDirectories) {
    for (const char* ok : {"foo", "a.b-c_9", "X"})
        EXPECT_TRUE(alpha_storage::is_valid_name(ok)) << ok;
    for (const char* bad : {"", ".", "..", ".hidden", "a/b", "a b", "../x", "a\\b"})
        EXPECT_FALSE(alpha_storage::is_valid_name(bad)) << bad;
    EXPECT_TRUE(alpha_storage::is_valid_name(std::string(200, 'a')));
    EXPECT_FALSE(alpha_storage::is_valid_name(std::string(201, 'a')));
}

TEST(AlphaStorage, MetadataYamlPreservesRunSettingsInOrderAndRoundTrips) {
    TempDir temp;
    auto metadata = sample_metadata();
    metadata.threads = 7;
    const std::string text = alpha_storage::metadata_yaml(metadata);
    EXPECT_EQ(text, "level: xml\nsimStart: 2016-01-04\nsimEnd: 2026-09-15\nisStart: 2016-01-04\n"
                    "osStart: 2024-01-01\nuniverse: TOP3000\nexecutionPrice: vwap\nthreads: 7\n");
    std::ofstream(temp.dir / "metadata.yml") << text;
    alpha_storage::Metadata read;
    ASSERT_TRUE(alpha_storage::read_metadata(temp.dir / "metadata.yml", &read));
    EXPECT_EQ(read.sim_end, metadata.sim_end);
    EXPECT_EQ(read.universe, metadata.universe);
    EXPECT_EQ(read.execution_price, metadata.execution_price);
    EXPECT_EQ(read.threads, 7u);

    auto empty_universe = metadata;
    empty_universe.universe = "";
    std::ofstream(temp.dir / "empty.yml") << alpha_storage::metadata_yaml(empty_universe);
    ASSERT_TRUE(alpha_storage::read_metadata(temp.dir / "empty.yml", &read));
    EXPECT_EQ(read.universe, "");
    EXPECT_EQ(read.threads, 7u);
}

TEST(AlphaStorage, DiscardedSaveLeavesNoStagingOrFinalDirectory) {
    TempDir temp;
    {
        alpha_storage::SaveTransaction save(temp.dir, "foo");
        save.write("alpha.xml", "<x/>");
        save.write(fs::path("results") / "rows" / "1.json", "{}");
        EXPECT_TRUE(fs::is_directory(save.staging()));
        EXPECT_EQ(save.staging().filename().string().rfind(".foo.tmp-", 0), 0u);
        // A failure before commit unwinds through the destructor.
    }
    EXPECT_EQ(entry_count(temp.dir), 0u);
}

TEST(AlphaStorage, CommitPublishesAtomicallyAndNeverReplaces) {
    TempDir temp;
    const std::string xml = "<alpha>\r\n  <!-- c --> <choice>5|10</choice>\t\n</alpha>";
    {
        alpha_storage::SaveTransaction save(temp.dir, "foo");
        save.write("alpha.xml", xml);
        save.write("metadata.yml", alpha_storage::metadata_yaml(sample_metadata()));
        EXPECT_TRUE(save.commit());
    }
    std::string read;
    ASSERT_TRUE(alpha_storage::read_file(temp.dir / "foo" / "alpha.xml", &read));
    EXPECT_EQ(read, xml);
    {
        alpha_storage::SaveTransaction save(temp.dir, "foo");
        save.write("alpha.xml", "<other/>");
        EXPECT_FALSE(save.commit());
    }
    ASSERT_TRUE(alpha_storage::read_file(temp.dir / "foo" / "alpha.xml", &read));
    EXPECT_EQ(read, xml);
    EXPECT_EQ(entry_count(temp.dir), 1u);
    // An empty directory of the same name also blocks publication.
    fs::create_directory(temp.dir / "bar");
    {
        alpha_storage::SaveTransaction save(temp.dir, "bar");
        save.write("alpha.xml", "<x/>");
        EXPECT_FALSE(save.commit());
    }
    EXPECT_TRUE(fs::is_empty(temp.dir / "bar"));
    EXPECT_EQ(entry_count(temp.dir), 2u);
}

TEST(AlphaStorage, ListSkipsHiddenIncompleteAndFlatEntriesAndSortsByName) {
    TempDir temp;
    auto make = [&](const std::string& name, bool metadata) {
        fs::create_directories(temp.dir / name);
        std::ofstream(temp.dir / name / "alpha.xml") << "<data expr=\"close\"/>";
        if (metadata) std::ofstream(temp.dir / name / "metadata.yml") << alpha_storage::metadata_yaml(sample_metadata());
    };
    make("zeta", true);
    make("alpha", true);
    make("incomplete", false);
    make(".alpha.tmp-1", true);
    std::ofstream(temp.dir / "legacy.xml") << "<legacy/>";
    const auto alphas = alpha_storage::list(temp.dir);
    ASSERT_EQ(alphas.size(), 2u);
    EXPECT_EQ(alphas[0].name, "alpha");
    EXPECT_EQ(alphas[0].xml, "<data expr=\"close\"/>");
    EXPECT_EQ(alphas[0].metadata.os_start, "2024-01-01");
    EXPECT_EQ(alphas[1].name, "zeta");

    EXPECT_TRUE(alpha_storage::remove(temp.dir, "alpha"));
    EXPECT_FALSE(fs::exists(temp.dir / "alpha"));
    EXPECT_FALSE(alpha_storage::remove(temp.dir, "alpha"));
    EXPECT_FALSE(alpha_storage::remove(temp.dir, ".."));
}

TEST(AlphaStorage, PrepareDirectoryCreatesAndRejectsUnwritablePaths) {
    TempDir temp;
    alpha_storage::prepare_directory(temp.dir / "a" / "b");
    EXPECT_TRUE(fs::is_directory(temp.dir / "a" / "b"));
    EXPECT_EQ(entry_count(temp.dir / "a" / "b"), 0u);
    std::ofstream(temp.dir / "file") << "x";
    EXPECT_THROW(alpha_storage::prepare_directory(temp.dir / "file" / "alphas"), std::runtime_error);
    if (::geteuid() != 0) {
        fs::create_directory(temp.dir / "readonly");
        fs::permissions(temp.dir / "readonly", fs::perms::owner_read | fs::perms::owner_exec);
        EXPECT_THROW(alpha_storage::prepare_directory(temp.dir / "readonly"), std::runtime_error);
        fs::permissions(temp.dir / "readonly", fs::perms::owner_all);
    }
}

TEST(AlphaStorage, PositionsAreSortedByDateThenIdAndSkipZeroOrNonFinite) {
    TempDir temp;
    alpha_storage::PositionsWriter writer({"20", "100", "3"});
    const double nan = std::numeric_limits<double>::quiet_NaN();
    writer.add_bar(86400000LL * 10, {0.25, -0.5, 0.25});
    writer.add_bar(86400000LL * 11, {0.0, nan, 1.0});
    fs::create_directory(temp.dir / "positions");
    writer.write(temp.dir / "positions" / "7.parquet");

    auto input = arrow::io::ReadableFile::Open((temp.dir / "positions" / "7.parquet").string()).ValueOrDie();
    std::unique_ptr<parquet::arrow::FileReader> reader;
    ASSERT_TRUE(parquet::arrow::OpenFile(input, arrow::default_memory_pool(), &reader).ok());
    std::shared_ptr<arrow::Table> table;
    ASSERT_TRUE(reader->ReadTable(&table).ok());
    table = table->CombineChunks().ValueOrDie();
    ASSERT_EQ(table->num_rows(), 4);
    ASSERT_EQ(table->schema()->field(0)->name(), "date");
    EXPECT_TRUE(table->schema()->field(0)->type()->Equals(arrow::date32()));
    EXPECT_EQ(table->schema()->field(1)->name(), "tradingItemId");
    EXPECT_TRUE(table->schema()->field(1)->type()->Equals(arrow::utf8()));
    EXPECT_EQ(table->schema()->field(2)->name(), "weight");
    EXPECT_TRUE(table->schema()->field(2)->type()->Equals(arrow::float64()));
    auto dates = std::static_pointer_cast<arrow::Date32Array>(table->column(0)->chunk(0));
    auto ids = std::static_pointer_cast<arrow::StringArray>(table->column(1)->chunk(0));
    auto weights = std::static_pointer_cast<arrow::DoubleArray>(table->column(2)->chunk(0));
    const std::vector<int32_t> expected_dates{10, 10, 10, 11};
    const std::vector<std::string> expected_ids{"100", "20", "3", "3"};
    const std::vector<double> expected_weights{-0.5, 0.25, 0.25, 1.0};
    for (int64_t i = 0; i < 4; ++i) {
        EXPECT_EQ(dates->Value(i), expected_dates[i]);
        EXPECT_EQ(ids->GetString(i), expected_ids[i]);
        EXPECT_EQ(weights->Value(i), expected_weights[i]);
    }
}

TEST(AlphaStorage, RemoveOnlyDeletesRealSavedAlphaDirectories) {
    TempDir temp;
    fs::create_directories(temp.dir / "saved");
    std::ofstream(temp.dir / "saved" / "alpha.xml") << "<x/>";
    std::ofstream(temp.dir / "saved" / "metadata.yml") << alpha_storage::metadata_yaml(sample_metadata());
    std::ofstream(temp.dir / "legacy.xml") << "<legacy/>";
    std::ofstream(temp.dir / "plainfile") << "x";
    fs::create_directories(temp.dir / "other" / "nested");
    std::ofstream(temp.dir / "other" / "alpha.xml") << "<x/>";  // no metadata.yml
    fs::create_directory_symlink(temp.dir / "saved", temp.dir / "link");
    for (const char* name : {"legacy.xml", "legacy", "plainfile", "other", "link", "missing"})
        EXPECT_FALSE(alpha_storage::remove(temp.dir, name)) << name;
    EXPECT_TRUE(fs::exists(temp.dir / "legacy.xml"));
    EXPECT_TRUE(fs::exists(temp.dir / "plainfile"));
    EXPECT_TRUE(fs::exists(temp.dir / "other" / "nested"));
    EXPECT_TRUE(fs::is_symlink(temp.dir / "link"));
    EXPECT_TRUE(fs::exists(temp.dir / "saved" / "alpha.xml"));
    EXPECT_TRUE(alpha_storage::remove(temp.dir, "saved"));
    EXPECT_FALSE(fs::exists(temp.dir / "saved"));
}

TEST(AlphaStorage, PrepareDirectoryRemovesOnlyStaleStagingDirectories) {
    TempDir temp;
    fs::create_directories(temp.dir / ".foo.tmp-123-0-9" / "results");
    std::ofstream(temp.dir / ".foo.tmp-123-0-9" / "alpha.xml") << "<x/>";
    fs::create_directories(temp.dir / ".keep");
    fs::create_directories(temp.dir / "foo.tmp-1");
    std::ofstream(temp.dir / ".bar.tmp-file") << "x";
    fs::create_directories(temp.dir / "target");
    std::ofstream(temp.dir / "target" / "keep") << "x";
    fs::create_directory_symlink(temp.dir / "target", temp.dir / ".baz.tmp-link");
    alpha_storage::prepare_directory(temp.dir);
    EXPECT_TRUE(fs::is_symlink(temp.dir / ".baz.tmp-link"));
    EXPECT_TRUE(fs::exists(temp.dir / "target" / "keep"));
    EXPECT_FALSE(fs::exists(temp.dir / ".foo.tmp-123-0-9"));
    EXPECT_TRUE(fs::exists(temp.dir / ".keep"));
    EXPECT_TRUE(fs::exists(temp.dir / "foo.tmp-1"));
    EXPECT_TRUE(fs::exists(temp.dir / ".bar.tmp-file"));
}

TEST(AlphaStorage, ListReportsMalformedMetadata) {
    TempDir temp;
    fs::create_directories(temp.dir / "bad");
    std::ofstream(temp.dir / "bad" / "alpha.xml") << "<x/>";
    std::ofstream(temp.dir / "bad" / "metadata.yml") << "level: [unclosed";
    fs::create_directories(temp.dir / "nometa");
    std::ofstream(temp.dir / "nometa" / "alpha.xml") << "<x/>";
    std::vector<std::string> malformed;
    EXPECT_TRUE(alpha_storage::list(temp.dir, &malformed).empty());
    EXPECT_EQ(malformed, std::vector<std::string>{"bad"});
}

TEST(AlphaStorage, LiveSaveSurvivesStartupSweep) {
    TempDir temp;
    alpha_storage::SaveTransaction save(temp.dir, "live");
    save.write("alpha.xml", "<x/>");
    save.write("metadata.yml", alpha_storage::metadata_yaml(sample_metadata()));
    save.write(fs::path("results") / "rows" / "1.json", "{}");
    alpha_storage::prepare_directory(temp.dir);  // another engine starting on the same alphas_dir
    EXPECT_TRUE(fs::is_directory(save.staging()));
    EXPECT_TRUE(fs::exists(save.staging() / "alpha.xml"));
    save.write(fs::path("results") / "rows" / "2.json", "{}");
    EXPECT_TRUE(save.commit());
    const auto alphas = alpha_storage::list(temp.dir);
    ASSERT_EQ(alphas.size(), 1u);
    EXPECT_EQ(alphas[0].name, "live");
    EXPECT_TRUE(fs::exists(temp.dir / "live" / "results" / "rows" / "2.json"));
}

TEST(AlphaStorage, SweepKeepsLockedStagingAndReapsAfterRelease) {
    TempDir temp;
    const fs::path staging = temp.dir / ".foo.tmp-1-0-9";
    fs::create_directories(staging / "results");
    const int fd = ::open(staging.c_str(), O_RDONLY | O_DIRECTORY | O_CLOEXEC);
    ASSERT_GE(fd, 0);
    ASSERT_EQ(::flock(fd, LOCK_EX | LOCK_NB), 0);
    alpha_storage::prepare_directory(temp.dir);
    EXPECT_TRUE(fs::exists(staging / "results"));
    ::close(fd);
    alpha_storage::prepare_directory(temp.dir);
    EXPECT_FALSE(fs::exists(staging));
}

TEST(AlphaStorage, WriteFailsClosedWhenStagingRemoved) {
    TempDir temp;
    {
        alpha_storage::SaveTransaction save(temp.dir, "gone");
        save.write("alpha.xml", "<x/>");
        fs::remove_all(save.staging());
        EXPECT_THROW(save.write("alpha.xml", "<x/>"), std::exception);
        EXPECT_THROW(save.write(fs::path("results") / "rows" / "1.json", "{}"), std::exception);
        EXPECT_THROW(save.prepare(fs::path("positions") / "1.parquet"), std::exception);
        EXPECT_THROW(save.commit(), std::exception);
    }
    EXPECT_FALSE(fs::exists(temp.dir / "gone"));
    EXPECT_EQ(entry_count(temp.dir), 0u);
}

TEST(AlphaStorage, EmbeddedMetadataRoundTripsAndFrontendGetsOnlyOperations) {
    TempDir temp;
    auto metadata = sample_metadata();
    metadata.threads = 7;
    const auto xml = alpha_storage::document_xml("<!-- keep --><data expr=\"close / adj_factor\" delay=\"1\"/><rank/>", metadata);
    EXPECT_EQ(xml.find("version="), std::string::npos);
    EXPECT_EQ(xml.find("<storage"), std::string::npos);
    EXPECT_EQ(xml.find("level="), std::string::npos);
    fs::create_directories(temp.dir / "saved");
    std::ofstream(temp.dir / "saved/alpha.xml") << xml;
    // Stale YAML must not override embedded settings.
    auto stale = metadata;
    stale.universe = "WRONG";
    std::ofstream(temp.dir / "saved/metadata.yml") << alpha_storage::metadata_yaml(stale);
    auto rows = alpha_storage::list(temp.dir);
    ASSERT_EQ(rows.size(), 1u);
    EXPECT_EQ(rows[0].metadata.universe, "TOP3000");
    EXPECT_EQ(rows[0].metadata.threads, 7u);
    EXPECT_EQ(rows[0].metadata.level, "xml");
    EXPECT_EQ(rows[0].xml.find("metadata"), std::string::npos);
    EXPECT_NE(rows[0].xml.find("<!-- keep -->"), std::string::npos);
    fs::create_directories(temp.dir / "saved/results");
    std::ofstream(temp.dir / "saved/results/index.json") << "{}";
    EXPECT_EQ(alpha_storage::list(temp.dir)[0].metadata.level, "results");
    fs::create_directories(temp.dir / "saved/positions");
    EXPECT_EQ(alpha_storage::list(temp.dir)[0].metadata.level, "positions");
    fs::remove(temp.dir / "saved/metadata.yml");
    EXPECT_TRUE(alpha_storage::remove(temp.dir, "saved"));
}

TEST(AlphaStorage, MalformedEmbeddedMetadataDoesNotFallBackToYaml) {
    TempDir temp;
    fs::create_directories(temp.dir / "bad");
    std::ofstream(temp.dir / "bad/alpha.xml") << "<alpha><pipeline><rank/></pipeline><metadata><simulation/></metadata></alpha>";
    std::ofstream(temp.dir / "bad/metadata.yml") << alpha_storage::metadata_yaml(sample_metadata());
    std::vector<std::string> malformed;
    EXPECT_TRUE(alpha_storage::list(temp.dir, &malformed).empty());
    EXPECT_EQ(malformed, std::vector<std::string>{"bad"});
}

TEST(AlphaStorage, ResavePreservesDescriptiveMetadataAndReplacesSimulation) {
    const std::string xml = "<alpha><pipeline><rank/></pipeline><metadata><simulation simStart=\"old\"/>"
                            "<author>jgo</author><description>A &amp; B</description><research>/notes/a.md</research></metadata></alpha>";
    const auto saved = alpha_storage::document_xml(xml, sample_metadata());
    EXPECT_NE(saved.find("<author>jgo</author>"), std::string::npos);
    EXPECT_NE(saved.find("A &amp; B"), std::string::npos);
    alpha_storage::Metadata read;
    ASSERT_TRUE(alpha_storage::read_document(saved, &read));
    EXPECT_EQ(read.sim_start, sample_metadata().sim_start);
}
