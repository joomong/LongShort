import React from 'react';
import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';

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

    expect(screen.getByText('Alpha Ranking (2)')).toBeInTheDocument();
    expect(screen.getByText('sortedBy=sharpe')).toBeInTheDocument();

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
    expect(cells[5]).toHaveTextContent('2.00');
    expect(headers[11]).toHaveTextContent('avgBet');
    expect(cells[11]).toHaveTextContent('5.25');
    expect(headers[14]).toHaveTextContent('minBet');
    expect(cells[14]).toHaveTextContent('1.00');
  });
});
