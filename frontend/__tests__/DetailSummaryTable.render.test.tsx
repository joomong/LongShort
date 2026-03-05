import React from 'react';
import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

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
        alpha={multiAlphaFixture.alphas[0]}
      />
    );

    expect(screen.getByText('Yearly Table')).toBeInTheDocument();
    expect(screen.getByText('2024')).toBeInTheDocument();

    // Long SM = 0.62 * 100 = 62.0%
    expect(screen.getByText('62.0%')).toBeInTheDocument();
    // Short SM = 0.38 * 100 = 38.0%
    expect(screen.getByText('38.0%')).toBeInTheDocument();
    // PnL = +2.2%
    expect(screen.getByText('+2.2%')).toBeInTheDocument();
    // Ann.Ret = +268.1%
    expect(screen.getByText('+268.1%')).toBeInTheDocument();
    // Sharpe = 6.00
    expect(screen.getByText('6.00')).toBeInTheDocument();
    // Max DD = -1.8%
    expect(screen.getByText('-1.8%')).toBeInTheDocument();
    // Daily TVR = 0.26 * 100 = 26.0
    expect(screen.getByText('26.0')).toBeInTheDocument();
    // Margin = 0.028 -> 0.0280
    expect(screen.getByText('0.0280')).toBeInTheDocument();
  });

  it('renders empty state when no yearly data', () => {
    const alphaNoYearly = { ...multiAlphaFixture.alphas[0], yearly: undefined };
    render(
      <DetailSummaryTable alpha={alphaNoYearly} />
    );

    expect(screen.getByText('No yearly summary (missing yearly data from engine)')).toBeInTheDocument();
  });
});
