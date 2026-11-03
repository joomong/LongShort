import React from 'react';
import { act, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SignalHeatmap from '@/components/SignalHeatmap';
import { useRunStore } from '@/runStore';
import { candidateFixture, contextFixture } from './metricFixtures';
vi.mock('@/chart', async (original) => ({
  ...(await original<typeof import('@/chart')>()),
  useContainerSize: () => ({ width: 100, height: 60 }),
}));
const initial = useRunStore.getState();
const contexts = new WeakMap<HTMLCanvasElement, ReturnType<typeof context>>();
function context() {
  return {
    createImageData: vi.fn((w: number, h: number) => ({
      data: new Uint8ClampedArray(w * h * 4),
      width: w,
      height: h,
    })),
    putImageData: vi.fn(),
    getImageData: vi.fn(),
    clearRect: vi.fn(),
    strokeRect: vi.fn(),
    strokeStyle: '',
    lineWidth: 1,
  };
}
let getContext: ReturnType<typeof vi.spyOn>;
beforeEach(() => {
  useRunStore.setState(initial, true);
  useRunStore.setState({
    viewMode: 'single',
    selectedSignalAlphaIndex: 0,
    result: {
      context: contextFixture,
      sourceXml: '<x/>',
      combined: null,
      alphas: [
        {
          ...candidateFixture(0),
          signals: {
            plot: {
              timestamps: [1000],
              symbols: ['A', 'B'],
              values: [[0, 30]],
              centers: [0],
              scales: [10],
              rowStride: 1,
              sampled: false,
            },
          },
        },
      ],
    },
  });
  getContext = vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockImplementation(function (
    this: HTMLCanvasElement,
  ) {
    let ctx = contexts.get(this);
    if (!ctx) {
      ctx = context();
      contexts.set(this, ctx);
    }
    return ctx as unknown as CanvasRenderingContext2D;
  });
});
afterEach(() => getContext.mockRestore());
describe('server-statistics heatmap rendering', () => {
  it('colors raw values with server statistics instead of calculating a new population', () => {
    render(<SignalHeatmap />);
    const canvas = screen.getByLabelText('Signal heatmap') as HTMLCanvasElement,
      ctx = contexts.get(canvas)!;
    const image = ctx.putImageData.mock.calls[0][0] as {
      data: Uint8ClampedArray;
      width: number;
      height: number;
    };
    const o = (image.height - 1) * image.width * 4;
    expect(Array.from(image.data.slice(o, o + 3))).toEqual([214, 39, 40]);
    expect(useRunStore.getState().result?.alphas[0].signals?.plot?.values).toEqual([[0, 30]]);
  });
  it('updates selection on its overlay without restoring or redrawing base pixels', () => {
    render(<SignalHeatmap />);
    const base = contexts.get(screen.getByLabelText('Signal heatmap') as HTMLCanvasElement)!;
    const overlay = contexts.get(screen.getByTestId('heatmap-selection') as HTMLCanvasElement)!;
    const draws = base.putImageData.mock.calls.length;
    act(() => useRunStore.getState().setSelectedCell('B', 1000));
    expect(overlay.strokeRect).toHaveBeenCalled();
    expect(base.putImageData).toHaveBeenCalledTimes(draws);
    expect(base.getImageData).not.toHaveBeenCalled();
  });
  it('does not allocate or draw while hidden, and resumes with current data', () => {
    useRunStore.setState({ viewMode: 'detail' });
    render(<SignalHeatmap />);
    const base = contexts.get(screen.getByLabelText('Signal heatmap') as HTMLCanvasElement);
    expect(base).toBeUndefined();
    act(() => useRunStore.setState({ viewMode: 'single' }));
    const visible = contexts.get(screen.getByLabelText('Signal heatmap') as HTMLCanvasElement)!;
    expect(visible.createImageData).toHaveBeenCalledOnce();
  });
});
