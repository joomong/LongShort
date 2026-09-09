#pragma once
// Review harness only: adapt the repository's newer OpenFile overload to the
// locally installed Arrow 18 API. No loader conversion/filtering logic changes.
#include <parquet/arrow/reader.h>
namespace parquet::arrow {
inline ::arrow::Result<std::unique_ptr<FileReader>> OpenFile(
    std::shared_ptr<::arrow::io::RandomAccessFile> file, ::arrow::MemoryPool* pool) {
    std::unique_ptr<FileReader> reader;
    auto status=OpenFile(std::move(file),pool,&reader);
    if (!status.ok()) return status;
    return reader;
}
}
