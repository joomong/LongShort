# PeriodResult Migration Plan: pandas Series → numpy arrays

## Overview
Complete migration from pandas Series to pure numpy arrays for PeriodResult class and related functionality. No legacy compatibility - clean replacement.

## Target Structure

### Before
```python
@dataclass(frozen=True)
class PeriodResult:
    metrics: PeriodMetrics
    timeseries: np.array
    ret_series: pd.Series
    turnover_series: pd.Series
    min_bet_series: pd.Series
    max_weight_series: pd.Series
    long_ratio: float = 0.0
    short_ratio: float = 0.0
```

### After
```python
@dataclass(frozen=True)
class PeriodResult:
    metrics: PeriodMetrics
    timestamps: np.ndarray  # datetime64[ns, UTC]
    ret_series: np.ndarray
    turnover_series: np.ndarray
    min_bet_series: np.ndarray
    max_weight_series: np.ndarray
    long_ratio: float = 0.0
    short_ratio: float = 0.0
```

## Migration Map: pandas → numpy

### Core Operations
| pandas operation | numpy equivalent | Usage location |
|-----------------|------------------|----------------|
| `series.fillna(0.0)` | `np.nan_to_num(arr, nan=0.0)` | _compute_metrics |
| `series.mean()` | `np.nanmean(arr)` | _compute_metrics |
| `series.max()` | `np.nanmax(arr)` | _compute_metrics |
| `series.std()` | `np.nanstd(arr)` | _compute_metrics |
| `series.sum()` | `np.nansum(arr)` | _compute_metrics |
| `series.cumprod()` | `np.cumprod(arr)` | _compute_metrics |
| `series.iloc[-1]` | `arr[-1]` | _compute_metrics |
| `series.empty` | `arr.size == 0` | _compute_metrics |
| `len(series)` | `arr.shape[0]` | _compute_metrics |
| `series.index` | separate timestamps array | multiple locations |
| `series.values` | `arr` (already numpy) | multiple locations |
| `series.items()` | `zip(timestamps, arr)` | serialization |

### Timezone Handling
| pandas operation | numpy equivalent |
|-----------------|------------------|
| `pd.DatetimeIndex(arr).tz_localize("UTC")` | `arr.astype('datetime64[ns]').astype('datetime64[ns, UTC]')` |
| `series.index < split_ts` | `timestamps < split_timestamp` |
| `pd.Timestamp(date, tz="UTC")` | `np.datetime64(date + 'T00:00:00', 'ns')` |

## File Changes Required

### 1. framework/backtester.py
- Update PeriodResult dataclass definition
- Update _compute_metrics function signature and implementation
- Update _build_period_result function
- Update _split_series_by_date → _split_arrays_by_date
- Update Backtester.run() return values
- Update IR/corr calculation functions

### 2. framework/parallel.py
- Update shared memory result structure
- Update worker function to handle numpy arrays
- Update result reconstruction

### 3. api/services/backtest_service.py
- Update serialization functions
- Update series → dict conversions
- Update heatmap and distribution handling

### 4. Test files (all)
- Update test assertions to expect numpy arrays
- Update mock data creation
- Update expected return types

## Implementation Strategy

### Phase 1: Core Structure
1. Update PeriodResult dataclass
2. Create numpy utility functions
3. Update _compute_metrics

### Phase 2: Backtester Engine
1. Update Backtester.run() to return numpy arrays
2. Update _build_period_result and _split_arrays_by_date
3. Update IR/corr calculations

### Phase 3: Parallel Processing
1. Update shared memory layout
2. Update worker functions
3. Update result reconstruction

### Phase 4: API Serialization
1. Update series → dict conversion functions
2. Update heatmap/distribution processing
3. Ensure identical JSON output

### Phase 5: Test Updates
1. Update all test files
2. Ensure identical functionality
3. Performance validation

## Key Technical Details

### Datetime Handling
```python
# Convert numpy datetime64[ns] to UTC timestamps
def ensure_utc_timestamps(timestamps: np.ndarray) -> np.ndarray:
    """Convert timestamps to datetime64[ns, UTC]."""
    if timestamps.dtype.kind != 'M':
        raise ValueError("Timestamps must be datetime64")
    # Ensure UTC timezone
    return timestamps.astype('datetime64[ns]').astype('datetime64[ns, UTC]')
```

### Series Splitting
```python
def _split_arrays_by_date(timestamps: np.ndarray, arrays: dict, split_date: str) -> tuple:
    """Split arrays by date using numpy boolean indexing."""
    split_ts = np.datetime64(split_date + 'T00:00:00', 'ns')
    mask = timestamps < split_ts
    
    return tuple((arr[mask] for arr in arrays), (arr[~mask] for arr in arrays))
```

### Serialization
```python
def _arrays_to_dict(timestamps: np.ndarray, values: np.ndarray) -> Dict[str, float]:
    """Convert timestamp + value arrays to {date_str: value} dict."""
    # Convert datetime64[ns, UTC] to ISO strings
    date_strings = pd.DatetimeIndex(timestamps).strftime('%Y-%m-%d')
    return {str(date): float(val) for date, val in zip(date_strings, values)}
```

## Validation Criteria

### Functional Equivalence
- [ ] Identical metrics values (within floating point precision)
- [ ] Identical JSON API responses
- [ ] Identical visualization data
- [ ] All tests pass with updated expectations

### Performance Targets
- [ ] Memory usage reduction (eliminate pandas overhead)
- [ ] Faster serialization (direct array → dict conversion)
- [ ] No performance regression in backtest execution

### Code Quality
- [ ] No pandas imports in core backtesting logic
- [ ] Clean separation of concerns (numpy for computation, pandas only for I/O)
- [ ] Type hints updated for numpy arrays
- [ ] Documentation updated

## Rollback Plan

If issues arise:
1. Keep PeriodResult changes but add compatibility layer
2. Gradually migrate individual functions
3. Maintain pandas path as fallback during transition

The migration is designed to be atomic - either complete migration or rollback to previous state.