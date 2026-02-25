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
  it('renders yearly net/gross/sharpe/mdd/fee strings with fixed format', () => {
    useAlphaStore.setState({
      dataRange: {
        minDate: '2024-01-01',
        maxDate: '2024-12-31',
        transactionFeeRate: 0.001,
      },
    });

    render(
      <DetailSummaryTable
        alpha={multiAlphaFixture.alphas[1]}
        initialCapital={100000}
      />
    );

    expect(screen.getByText('1Y Table (calendar year)')).toBeInTheDocument();
    expect(screen.getByText('2024')).toBeInTheDocument();
    expect(screen.getAllByText('+2.2%').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText('6.00')).toBeInTheDocument();
    expect(screen.getByText('-1.8%')).toBeInTheDocument();
    expect(screen.getByText('62.0%')).toBeInTheDocument();
    expect(screen.getByText('38.0%')).toBeInTheDocument();
    expect(screen.getByText('+0.08% / $79')).toBeInTheDocument();
  });

  it('applies transactionFeeRate from store to fee impact output', () => {
    useAlphaStore.setState({
      dataRange: {
        minDate: '2024-01-01',
        maxDate: '2024-12-31',
        transactionFeeRate: 0.002,
      },
    });

    render(
      <DetailSummaryTable
        alpha={multiAlphaFixture.alphas[1]}
        initialCapital={100000}
      />
    );

    expect(screen.getByText('+0.16% / $159')).toBeInTheDocument();
  });
});
