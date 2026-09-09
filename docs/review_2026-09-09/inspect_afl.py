"""Read-only cache inventory and memory estimates; does not load all AFL factors.

Use: python inspect_afl.py --output /tmp/afl_inventory.json
Only selected sample footers/index columns and two boolean universe masks are read.
Memory numbers are dense array byte estimates, not measured process RSS.
"""
import argparse
import gc
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow.parquet as pq


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', type=Path, default=Path('/home/base/cache/cache_v1_cash_1d'))
    parser.add_argument('--start', default='2010-01-01')
    parser.add_argument('--end', default='2026-09-08')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = {'observed_at_utc': datetime.now(timezone.utc).isoformat(),
              'root': str(args.root), 'start': args.start, 'end': args.end,
              'directories': {}, 'universes': {}, 'samples': []}
    for name in ['AflData', 'AflData_M']:
        files = sorted((args.root / name).glob('*.parquet'))
        result['directories'][name] = {
            'parquet_files': len(files),
            'numeric_factor_files': sum(p.stem.isdigit() for p in files),
            'nonfactor_files': [p.name for p in files if not p.stem.isdigit()],
            'disk_bytes': sum(p.stat().st_size for p in files),
        }
    # Read only the date column for the price calendar.
    close_pf = pq.ParquetFile(args.root / 'BaseData/close.parquet')
    date_col = '__index_level_0__'
    close_dates = pd.DatetimeIndex(close_pf.read(columns=[date_col]).column(0).to_numpy())
    for universe in ['Top500', 'Top1000']:
        table = pq.ParquetFile(args.root / f'TopUniverseV1/{universe}.parquet').read()
        dates = pd.DatetimeIndex(table.column(date_col).to_numpy())
        keep = (dates >= args.start) & (dates <= args.end) & dates.isin(close_dates)
        # Exclude rows with no member rather than assuming the final row is incomplete.
        daily_counts = np.zeros(int(keep.sum()), dtype=np.int32)
        members = []
        for name in table.column_names:
            if name == date_col:
                continue
            col = table.column(name).to_numpy(zero_copy_only=False)
            if col.dtype == object:
                col = np.fromiter((bool(v) if v is not None else False for v in col), dtype=bool)
            active = col[keep]
            daily_counts += active.astype(np.int32)
            if active.any():
                members.append(name)
        active_days = daily_counts > 0
        selected_dates = dates[keep][active_days]
        t, n = len(selected_dates), len(members)
        result['universes'][universe] = {
            'T': t, 'N_union': n, 'first': str(selected_dates[0]), 'last': str(selected_dates[-1]),
            'average_mask_members': float(daily_counts[active_days].mean()),
            'members': members,
            'one_field_f32_bytes': t*n*4,
            'base_8_fields_f32_bytes': t*n*4*8,
            'afl_10_daily_fields_f32_bytes': t*n*4*10,
            'afl_50_daily_fields_f32_bytes': t*n*4*50,
            'afl_600_daily_fields_f32_bytes': t*n*4*600,
            'afl_600_daily_fields_f64_bytes': t*n*8*600,
        }
        del table
        gc.collect()
        print(universe, t, n, flush=True)
    for dataset in ['AflData', 'AflData_M']:
        for factor in ['100', '464', '1005']:
            path = args.root / dataset / f'{factor}.parquet'
            pf = pq.ParquetFile(path)
            schema = pf.schema_arrow
            dates = pd.DatetimeIndex(pf.read(columns=[date_col]).column(0).to_numpy())
            names = set(schema.names) - {date_col}
            selected = dates[(dates >= args.start) & (dates <= args.end)]
            sample = {'path': str(path), 'rows': pf.metadata.num_rows, 'symbol_columns': len(names),
                      'row_groups': pf.metadata.num_row_groups, 'value_type': str(schema.field(0).type),
                      'index_type': str(schema.field(date_col).type), 'first': str(dates.min()),
                      'last': str(dates.max()), 'unique_dates': len(dates.unique()),
                      'dates_monotonic': bool(dates.is_monotonic_increasing),
                      'rows_in_period': len(selected), 'footer_bytes': pf.metadata.serialized_size,
                      'median_calendar_gap_days': float(np.median(np.diff(dates.asi8))/86400e9),
                      'period_dense_f32_600_factors_by_universe_bytes': {},
                      'missing_union_symbols': {}}
            for universe, u in result['universes'].items():
                sample['missing_union_symbols'][universe] = len(set(u['members'])-names)
                sample['period_dense_f32_600_factors_by_universe_bytes'][universe] = len(selected)*u['N_union']*4*600
            sample['full_source_f32_600_factors_bytes'] = len(dates)*len(names)*4*600
            result['samples'].append(sample)
            print(dataset, factor, len(dates), dates.max(), flush=True)
            del pf, schema
            gc.collect()
    for u in result['universes'].values():
        del u['members']
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False)+'\n')


if __name__ == '__main__':
    main()
