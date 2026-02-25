import React from 'react';
import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';

import BacktestMetrics from '@/components/visualization/BacktestMetrics';
import { useAlphaStore } from '@/store/alphaStore';
import { singleAlphaFixture } from './pipelineFixtures';

const initialState = useAlphaStore.getState();

beforeEach(() => {
  useAlphaStore.setState(initialState, true);
});

describe('BacktestMetrics render formatting', () => {
  it('renders metric strings with sign, percent, and decimals', () => {
    useAlphaStore.setState({ result: singleAlphaFixture });

    render(<BacktestMetrics />);

    expect(screen.getAllByText('CAGR').length).toBeGreaterThanOrEqual(2);
    expect(screen.getByText('+947.5%')).toBeInTheDocument();
    expect(screen.getByText('5.06')).toBeInTheDocument();
    expect(screen.getByText('-2.0%')).toBeInTheDocument();
    expect(screen.getByText('27')).toBeInTheDocument();
    expect(screen.getByText('60.0%')).toBeInTheDocument();

    expect(screen.getByText('-60.3%')).toBeInTheDocument();
    expect(screen.getByText('-4.50')).toBeInTheDocument();
    expect(screen.getByText('-1.0%')).toBeInTheDocument();
  });
});
