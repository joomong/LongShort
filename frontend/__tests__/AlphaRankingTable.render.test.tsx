import React from 'react';
import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';

import AlphaRankingTable from '@/components/multi-alpha/AlphaRankingTable';
import { useAlphaStore } from '@/store/alphaStore';
import { multiAlphaFixture } from './pipelineFixtures';

const initialState = useAlphaStore.getState();

beforeEach(() => {
  useAlphaStore.setState(initialState, true);
});

describe('AlphaRankingTable rendering', () => {
  it('renders ranking rows with STAR-aligned columns', () => {
    useAlphaStore.setState({ result: multiAlphaFixture });

    render(<AlphaRankingTable />);

    expect(screen.getByText('Alpha Ranking (2)')).toBeInTheDocument();
    expect(screen.getByText('sortedBy=sharpe')).toBeInTheDocument();
    expect(screen.getByText('Combined')).toBeInTheDocument();
    expect(screen.getByText('alpha_1')).toBeInTheDocument();
    expect(screen.getByText('alpha_2')).toBeInTheDocument();

    // Combined row: margin from fixture = 0.000974 -> 0.0010
    const combinedRow = screen.getByText('Combined').closest('tr');
    expect(combinedRow).not.toBeNull();
    expect(within(combinedRow as HTMLElement).getByText('0.0010')).toBeInTheDocument();

    // alpha_1 row: margin = 0.001315 -> 0.0013
    const alpha1Row = screen.getByText('alpha_1').closest('tr');
    expect(alpha1Row).not.toBeNull();
    expect(within(alpha1Row as HTMLElement).getByText('0.0013')).toBeInTheDocument();
  });
});
