import React from 'react';
import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';

import DetailSummaryTable from '@/components/visualization/DetailSummaryTable';
import { useAlphaStore } from '@/store/alphaStore';
import { multiAlphaFixture } from './pipelineFixtures';

const initialState = useAlphaStore.getState();

beforeEach(() => {
  useAlphaStore.setState(initialState, true);
});

describe('DetailSummaryTable render output', () => {
  it('renders yearly columns from C++ yearly data', () => {
    render(
      <DetailSummaryTable
        alpha={{ ...multiAlphaFixture.alphas[0], yearly: multiAlphaFixture.alphas[0].yearly!.map(row => ({ ...row, ir: -0.452, fitness: -1.23456 })) }}
      />
    );

    expect(screen.getByRole('table', { name: 'Yearly performance' })).toBeInTheDocument();
    expect(screen.getAllByRole('columnheader').map(cell => cell.textContent)).toEqual([
      'year', 'long $M', 'short $M', 'bks $M', 'pnl $M', 'ann.ret', 'sharpe[ir]', 'max.dd', 'win/lose', 'daily tvr', 'margin', 'fitness',
    ]);
    const row = screen.getByText('2025').closest('tr')!;
    expect(row.querySelector('.text-metric-positive, .text-metric-negative')).toBeNull();
    expect(row).not.toHaveClass('border-b');
    // Fixed $20M report: exposure fractions become signed $M, PnL pp becomes $M; margin ratio becomes bps.
    expect(within(row).getAllByRole('cell').map(cell => cell.textContent)).toEqual([
      '2025', '10.00', '-10.00', '20.0', '-1.01', '-14.80%', '-7.174[-0.452]', '-5.05%', '—', '8.82%', '-66.60 bps', '-1.235',
    ]);
  });

  it('shows actual zero win rate, missing values as dashes, and newest year first', () => {
    const base = multiAlphaFixture.alphas[0];
    const row = base.yearly![0];
    render(<DetailSummaryTable alpha={{ ...base, yearly: [
      { ...row, year: 2023, winLose: 0, ir: 0 },
      { ...row, year: 2025, winLose: 62.5, ir: undefined },
      { ...row, year: 2024, winLose: null, ir: null },
    ] }} />);
    const rows = screen.getAllByRole('row').slice(1);
    expect(rows.map(row => within(row).getAllByRole('cell')[0].textContent)).toEqual(['2025', '2024', '2023']);
    expect(within(rows[0]).getByText('62.50%')).toBeInTheDocument();
    expect(within(rows[2]).getByText('0.00%')).toBeInTheDocument();
    expect(within(rows[1]).getAllByRole('cell')[8]).toHaveTextContent('—');
    expect(within(rows[0]).getAllByRole('cell')[6]).toHaveTextContent('[—]');
    expect(within(rows[1]).getAllByRole('cell')[6]).toHaveTextContent('[—]');
    expect(within(rows[2]).getAllByRole('cell')[6]).toHaveTextContent('[0.000]');
  });

  it('shows zero Fitness and keeps absent or nonfinite Fitness as dashes', () => {
    const base = multiAlphaFixture.alphas[0];
    const row = base.yearly![0];
    render(<DetailSummaryTable alpha={{ ...base, yearly: [
      { ...row, year: 2025, fitness: 0 },
      { ...row, year: 2024, fitness: undefined },
      { ...row, year: 2023, fitness: null },
      { ...row, year: 2022, fitness: Number.NaN },
    ] }} />);
    const rows = screen.getAllByRole('row').slice(1);
    expect(rows.map(row => within(row).getAllByRole('cell')[11].textContent)).toEqual(['0.000', '—', '—', '—']);
    expect(screen.getByRole('columnheader', { name: 'fitness' })).toHaveAttribute('title', expect.stringContaining('leverage 1'));
  });

  it('renders empty state when no yearly data', () => {
    const alphaNoYearly = { ...multiAlphaFixture.alphas[0], yearly: undefined };
    render(
      <DetailSummaryTable alpha={alphaNoYearly} />
    );

    expect(screen.getByText('No yearly summary (missing yearly data from engine)')).toBeInTheDocument();
  });
});
