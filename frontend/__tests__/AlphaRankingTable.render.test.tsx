import React from 'react';
import { describe, it, expect, beforeEach } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';

import AlphaRankingTable from '@/components/multi-alpha/AlphaRankingTable';
import { useAlphaStore } from '@/store/alphaStore';
import { multiAlphaMetricFixture } from './metricFixtures';

const initialState = useAlphaStore.getState();

beforeEach(() => {
  useAlphaStore.setState(initialState, true);
});

describe('AlphaRankingTable rendering', () => {
  it('renders ranking rows with STAR-aligned columns', () => {
    useAlphaStore.setState({ result: multiAlphaMetricFixture });

    render(<AlphaRankingTable />);

    expect(screen.getByRole('table', { name: 'Alpha results' })).toBeInTheDocument();
    expect(screen.queryByText(/Alpha Ranking/)).not.toBeInTheDocument();

    // Rows identified by rank-based ID (0, 1), no Name column
    const rows = screen.getAllByRole('button');
    expect(rows.length).toBeGreaterThanOrEqual(2);

    // First alpha row: ID=0, sharpe = 5.9995 -> 6.00
    const alpha0Row = screen.getByText('0').closest('tr');
    expect(alpha0Row).not.toBeNull();
    expect(within(alpha0Row as HTMLElement).getByText('6.00')).toBeInTheDocument();

    // Keep avgBet = average held symbols and minBet = minimum held symbols.
    const headers = within(screen.getAllByRole('row')[1]).getAllByRole('columnheader');
    const cells = within(alpha0Row as HTMLElement).getAllByRole('cell');
    expect(headers[2]).toHaveTextContent('avgBet');
    expect(cells[2]).toHaveTextContent('3.50');
    expect(headers[5]).toHaveTextContent('minBet');
    expect(cells[5]).toHaveTextContent('2');
    expect(headers[11]).toHaveTextContent('avgBet');
    expect(cells[11]).toHaveTextContent('5.25');
    expect(headers[14]).toHaveTextContent('minBet');
    expect(cells[14]).toHaveTextContent('1');
  });
});


describe('Reference table formatting', () => {
  it('keeps sign criteria as underlines, neutral turnover, and Sharpe400 without Sharpe80', () => {
    const alpha = multiAlphaMetricFixture.alphas[0];
    useAlphaStore.setState({ result: { ...multiAlphaMetricFixture, alphas: [{
      ...alpha,
      isMetrics: { ...alpha.isMetrics, avgMaxBet: 2.875, sharpe400: 1.234, sharpe80: -0.236 },
      osMetrics: { ...alpha.osMetrics, sharpe80: 0 },
    }] } });
    render(<AlphaRankingTable />);
    const row = screen.getByRole('button', { name: `View ${alpha.name}` });
    const cells = within(row).getAllByRole('cell');
    expect(cells[3]).toHaveTextContent('2.88%');
    expect(cells[4]).toHaveTextContent('60.00%');
    expect(cells[5]).toHaveTextContent(/^2$/);
    expect(cells[7]).toHaveTextContent('+947.50%');
    expect(cells[9]).toHaveTextContent('1.23');
    expect(cells[10]).toHaveTextContent('26.70%');
    expect(cells[7].firstElementChild).toHaveClass('decoration-metric-positive', 'underline');
    expect(cells[16].firstElementChild).toHaveClass('decoration-metric-negative');
    expect(cells[10].querySelector('.underline')).toBeNull();
    expect(cells[7]).toHaveClass('text-[#111111]');
    expect(row.querySelector('.text-metric-positive, .text-metric-negative')).toBeNull();
    expect(screen.getAllByRole('columnheader', { name: 'sharpe400' })).toHaveLength(2);
    expect(screen.queryByRole('columnheader', { name: 'sharpe80' })).not.toBeInTheDocument();
    expect(screen.getByRole('columnheader', { name: 'IS' })).toHaveAttribute('colspan', '9');
    expect(screen.getByRole('columnheader', { name: 'OS' })).toHaveAttribute('colspan', '9');
    expect(cells).toHaveLength(20);
    for (const header of screen.getAllByRole('columnheader', { name: 'quality' })) {
      expect(header).toHaveAttribute('title', expect.stringContaining('max net exposure'));
    }
  });

  it('keeps save selection independent of detail row activation', () => {
    useAlphaStore.setState({ result: multiAlphaMetricFixture });
    render(<AlphaRankingTable />);
    const row = screen.getByRole('button', { name: 'View alpha_0' });
    const saveSelection = within(row).getByRole('button', { name: 'Select alpha' });
    fireEvent.click(saveSelection);
    expect(useAlphaStore.getState().selectedAlphaIndices).toEqual([0]);
    expect(useAlphaStore.getState().selectedAlphaIndex).toBeNull();
    fireEvent.keyDown(saveSelection, { key: 'Enter' });
    expect(useAlphaStore.getState().selectedAlphaIndex).toBeNull();
    fireEvent.keyDown(row, { key: 'Enter' });
    expect(useAlphaStore.getState().selectedAlphaIndex).toBe(0);
    expect(row).toHaveClass('bg-[#e0ddf5]');
    expect(saveSelection).toHaveAttribute('aria-pressed', 'true');
  });
});


describe('Batch save selection', () => {
  it('places IDs before circles and selects all without switching the detail alpha', () => {
    const combined = { ...multiAlphaMetricFixture.alphas[0], name: 'combined' };
    useAlphaStore.setState({ result: { ...multiAlphaMetricFixture, combined } });
    render(<AlphaRankingTable />);
    const batch = screen.getByRole('button', { name: 'View combined alpha' });
    const cells = within(batch).getAllByRole('cell');
    expect(cells[0]).toHaveTextContent(/^batch$/);
    const selectAll = within(cells[1]).getByRole('button', { name: 'Select all alphas' });
    const alphaRow = screen.getByRole('button', { name: 'View alpha_0' });
    const alphaCells = within(alphaRow).getAllByRole('cell');
    expect(alphaCells[0]).toHaveTextContent(/^0$/);
    fireEvent.click(within(alphaCells[1]).getByRole('button', { name: 'Select alpha' }));
    expect(selectAll).toHaveAttribute('aria-pressed', 'mixed');
    fireEvent.keyDown(selectAll, { key: 'Enter' });
    expect(useAlphaStore.getState().selectedAlphaIndex).toBeNull();
    fireEvent.click(selectAll);
    expect(useAlphaStore.getState().selectedAlphaIndices).toEqual([0, 1]);
    expect(useAlphaStore.getState().selectedAlphaIndex).toBeNull();
    expect(selectAll).toHaveAttribute('aria-pressed', 'true');
    expect(selectAll).toHaveAccessibleName('Deselect all alphas');
    fireEvent.click(selectAll);
    expect(useAlphaStore.getState().selectedAlphaIndices).toEqual([]);
    expect(selectAll).toHaveAttribute('aria-pressed', 'false');
    fireEvent.click(within(batch).getByText('batch'));
    expect(useAlphaStore.getState().selectedAlphaIndex).toBe(-1);
  });
});
