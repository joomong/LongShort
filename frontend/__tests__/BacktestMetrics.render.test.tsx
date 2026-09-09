import React from 'react';
import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

import BacktestMetrics from '@/components/visualization/BacktestMetrics';
import { useAlphaStore } from '@/store/alphaStore';
import { singleAlphaMetricFixture } from './metricFixtures';

const initialState = useAlphaStore.getState();

beforeEach(() => {
  useAlphaStore.setState(initialState, true);
});

describe('BacktestMetrics render formatting', () => {
  it('renders metric strings with sign, percent, and decimals', () => {
    useAlphaStore.setState({ result: singleAlphaMetricFixture });

    render(<BacktestMetrics />);

    // Column headers present
    expect(screen.getAllByText('Return').length).toBeGreaterThanOrEqual(2);
    expect(screen.getAllByText('Sharpe').length).toBeGreaterThanOrEqual(2);

    // IS metrics: Return, Sharpe, Turnover, MinBet, MaxWt, Alpha, Margin
    expect(screen.getByText('+947.5%')).toBeInTheDocument();
    expect(screen.getByText('5.06')).toBeInTheDocument();
    expect(screen.getByText('26.7%')).toBeInTheDocument();
    expect(screen.getByText('3.50')).toBeInTheDocument();
    expect(screen.getByText('60.0%')).toBeInTheDocument();
    // Alpha = activeBarCount (legacy wire nonzeroPosition) = 3
    expect(screen.getByText('3')).toBeInTheDocument();
    // Margin = 0.000974 -> 0.0010
    expect(screen.getByText('0.0010')).toBeInTheDocument();

    // OS metrics: Return, Sharpe
    expect(screen.getByText('-60.3%')).toBeInTheDocument();
    expect(screen.getByText('-4.50')).toBeInTheDocument();
  });
});
