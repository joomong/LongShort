import React from 'react';
import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import DetailSummaryTable from '@/components/YearlyMetricsTable';
import { contextFixture, candidateFixture } from './metricFixtures';
import type { YearlyMetrics } from '@/types';
const year: YearlyMetrics = {
  year: 2025,
  longRatio: 0.5,
  shortRatio: 0.5,
  pnl: -0.0505,
  annualReturn: -0.148,
  sharpe: -7.174,
  informationRatio: -0.452,
  maxDrawdown: -0.0505,
  winRate: 0.625,
  turnover: 0.0882,
  margin: -0.00666,
  fitness: -1.235,
};
describe('yearly report display', () => {
  it('uses context report notional, fraction formatting and engine-provided statistics', () => {
    render(
      <DetailSummaryTable
        alpha={{ ...candidateFixture(0), yearly: [year] }}
        context={contextFixture}
      />,
    );
    expect(screen.getAllByRole('columnheader').map((c) => c.textContent)).toEqual([
      'year',
      'long $M',
      'short $M',
      'bks $M',
      'pnl $M',
      'ann.ret',
      'sharpe[ir]',
      'max.dd',
      'win/lose',
      'daily tvr',
      'margin',
      'fitness',
    ]);
    const row = screen.getByText('2025').closest('tr')!;
    expect(
      within(row)
        .getAllByRole('cell')
        .map((c) => c.textContent),
    ).toEqual([
      '2025',
      '10.00',
      '-10.00',
      '20.0',
      '-1.01',
      '-14.80%',
      '-7.174[-0.452]',
      '-5.05%',
      '62.50%',
      '8.82%',
      '-66.60 bps',
      '-1.235',
    ]);
    expect(row.querySelector('.text-metric-positive,.text-metric-negative')).toBeNull();
  });
  it('shows real zero values, nonfinite unavailable values, newest year first', () => {
    render(
      <DetailSummaryTable
        alpha={{
          ...candidateFixture(0),
          yearly: [
            { ...year, year: 2023, winRate: 0, informationRatio: 0, fitness: 0 },
            { ...year, year: 2025 },
            { ...year, year: 2024, winRate: NaN, informationRatio: NaN, fitness: NaN },
          ],
        }}
        context={contextFixture}
      />,
    );
    const rows = screen.getAllByRole('row').slice(1);
    expect(rows.map((r) => within(r).getAllByRole('cell')[0].textContent)).toEqual([
      '2025',
      '2024',
      '2023',
    ]);
    expect(within(rows[2]).getByText('0.00%')).toBeInTheDocument();
    expect(within(rows[2]).getAllByRole('cell')[11]).toHaveTextContent('0.000');
    expect(within(rows[1]).getAllByRole('cell')[8]).toHaveTextContent('—');
    expect(within(rows[1]).getAllByRole('cell')[6]).toHaveTextContent('[—]');
  });
  it('scales display money only, without changing performance ratios', () => {
    render(
      <DetailSummaryTable
        alpha={{ ...candidateFixture(0), yearly: [year] }}
        context={{ ...contextFixture, reportNotional: 10_000_000 }}
      />,
    );
    const cells = within(screen.getByText('2025').closest('tr')!).getAllByRole('cell');
    expect(cells[1]).toHaveTextContent('5.00');
    expect(cells[4]).toHaveTextContent('-0.51');
    expect(cells[5]).toHaveTextContent('-14.80%');
  });
  it('renders an explicit empty report when no yearly data exists', () => {
    render(<DetailSummaryTable alpha={candidateFixture(0)} context={contextFixture} />);
    expect(
      screen.getByText('No yearly summary (missing yearly data from engine)'),
    ).toBeInTheDocument();
  });
});
