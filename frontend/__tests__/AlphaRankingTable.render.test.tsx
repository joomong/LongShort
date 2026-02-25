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
  it('renders ranking rows and combined IR/Corr fallback text', () => {
    useAlphaStore.setState({ result: multiAlphaFixture });

    render(<AlphaRankingTable />);

    expect(screen.getByText('Alpha Ranking (2)')).toBeInTheDocument();
    expect(screen.getByText('sortedBy=sharpe')).toBeInTheDocument();
    expect(screen.getByText('Combined')).toBeInTheDocument();
    expect(screen.getByText('alpha_1')).toBeInTheDocument();
    expect(screen.getByText('alpha_2')).toBeInTheDocument();

    const combinedRow = screen.getByText('Combined').closest('tr');
    expect(combinedRow).not.toBeNull();
    expect(within(combinedRow as HTMLElement).getAllByText('-').length).toBeGreaterThanOrEqual(2);

    const alpha1Row = screen.getByText('alpha_1').closest('tr');
    expect(alpha1Row).not.toBeNull();
    expect(within(alpha1Row as HTMLElement).getByText('0.11')).toBeInTheDocument();
    expect(within(alpha1Row as HTMLElement).getByText('0.42')).toBeInTheDocument();
  });
});
