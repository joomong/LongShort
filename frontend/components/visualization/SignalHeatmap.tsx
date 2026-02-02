'use client';

import { useRef, useEffect, useCallback, useState } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { useThemeStore } from '@/store/themeStore';
import { getThemeColors } from '@/lib/themeColors';
import { signalToColor } from '@/lib/colorScale';

const MIN_CELL_WIDTH = 1;
const MIN_CELL_HEIGHT = 2;

interface TooltipInfo {
  x: number;
  y: number;
  symbol: string;
  timestamp: string;
  value: number;
}

export default function SignalHeatmap() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [tooltip, setTooltip] = useState<TooltipInfo | null>(null);
  // Store computed cell sizes so mouse handlers can use them
  const cellSizeRef = useRef({ w: MIN_CELL_WIDTH, h: MIN_CELL_HEIGHT });
  const { result, selectedSymbol, selectedTimestamp, setSelectedCell } = useAlphaStore();
  const { theme } = useThemeStore();

  const draw = useCallback(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container || !result?.heatmap || !result.signalRange) return;

    const ctx = canvas.getContext('2d');
    if (!ctx) return;

    const colors = getThemeColors();
    const { heatmap } = result;
    const { timestamps, symbols, values } = heatmap;
    if (timestamps.length === 0 || symbols.length === 0) return;

    // Fit canvas to container
    const containerWidth = container.clientWidth;
    const containerHeight = container.clientHeight;

    const cellW = Math.max(MIN_CELL_WIDTH, containerWidth / timestamps.length);
    const cellH = Math.max(MIN_CELL_HEIGHT, containerHeight / symbols.length);
    cellSizeRef.current = { w: cellW, h: cellH };

    const width = Math.ceil(cellW * timestamps.length);
    const height = Math.ceil(cellH * symbols.length);

    canvas.width = width;
    canvas.height = height;
    canvas.style.width = `${containerWidth}px`;
    canvas.style.height = `${Math.max(containerHeight, height)}px`;

    // Clear canvas
    ctx.fillStyle = colors.chartBackground;
    ctx.fillRect(0, 0, width, height);

    // Draw heatmap cells (X=time, Y=symbol)
    // Per-timestamp (column) min/max for cross-sectional color mapping
    const colMin = new Float64Array(timestamps.length);
    const colMax = new Float64Array(timestamps.length);
    for (let t = 0; t < timestamps.length; t++) {
      let lo = Infinity, hi = -Infinity;
      const col = values[t];
      if (col) {
        for (let s = 0; s < symbols.length; s++) {
          const v = col[s];
          if (typeof v === 'number' && isFinite(v)) {
            if (v < lo) lo = v;
            if (v > hi) hi = v;
          }
        }
      }
      colMin[t] = isFinite(lo) ? lo : 0;
      colMax[t] = isFinite(hi) ? hi : 0;
    }

    symbols.forEach((_, symbolIdx) => {
      timestamps.forEach((_, timeIdx) => {
        const value = values[timeIdx]?.[symbolIdx];
        const color = signalToColor(typeof value === 'number' ? value : NaN, colMin[timeIdx], colMax[timeIdx]);

        const x = Math.floor(timeIdx * cellW);
        const y = Math.floor(symbolIdx * cellH);
        const w = Math.ceil(cellW);
        const h = Math.ceil(cellH);

        ctx.fillStyle = color;
        ctx.fillRect(x, y, w, h);
      });
    });

    // Draw selection highlight
    if (selectedSymbol && selectedTimestamp) {
      const symbolIdx = symbols.indexOf(selectedSymbol);
      const timeIdx = timestamps.indexOf(selectedTimestamp);
      if (symbolIdx !== -1 && timeIdx !== -1) {
        const x = Math.floor(timeIdx * cellW);
        const y = Math.floor(symbolIdx * cellH);
        ctx.strokeStyle = colors.accent;
        ctx.lineWidth = 2;
        ctx.strokeRect(x, y, Math.ceil(cellW), Math.ceil(cellH));
      }
    }
  }, [result, selectedSymbol, selectedTimestamp, theme]);

  useEffect(() => {
    draw();
  }, [draw]);

  // Redraw on container resize
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver(() => draw());
    observer.observe(container);
    return () => observer.disconnect();
  }, [draw]);

  const getCell = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas || !result?.heatmap) return null;

    const rect = canvas.getBoundingClientRect();
    // Account for CSS scaling (canvas pixel size vs display size)
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    const x = (e.clientX - rect.left) * scaleX;
    const y = (e.clientY - rect.top) * scaleY;

    const { w, h } = cellSizeRef.current;
    const timeIdx = Math.floor(x / w);
    const symbolIdx = Math.floor(y / h);
    const { heatmap } = result;

    if (timeIdx >= 0 && timeIdx < heatmap.timestamps.length &&
        symbolIdx >= 0 && symbolIdx < heatmap.symbols.length) {
      return { timeIdx, symbolIdx };
    }
    return null;
  }, [result]);

  const handleMouseMove = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const cell = getCell(e);
    if (!cell || !result?.heatmap) { setTooltip(null); return; }

    const { heatmap } = result;
    const value = heatmap.values[cell.timeIdx]?.[cell.symbolIdx];
    setTooltip({
      x: e.clientX,
      y: e.clientY,
      symbol: heatmap.symbols[cell.symbolIdx],
      timestamp: heatmap.timestamps[cell.timeIdx],
      value: typeof value === 'number' ? value : 0,
    });
  }, [result, getCell]);

  const handleMouseLeave = useCallback(() => {
    setTooltip(null);
  }, []);

  const handleClick = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const cell = getCell(e);
    if (!cell || !result?.heatmap) return;

    const symbol = result.heatmap.symbols[cell.symbolIdx];
    const timestamp = result.heatmap.timestamps[cell.timeIdx];

    if (symbol && timestamp) {
      if (symbol === selectedSymbol && timestamp === selectedTimestamp) {
        setSelectedCell(null, null);
      } else {
        setSelectedCell(symbol, timestamp);
      }
    }
  }, [result, selectedSymbol, selectedTimestamp, setSelectedCell, getCell]);

  if (!result) {
    return (
      <div
        className="w-full border-b border-border bg-chart-background"
        style={{ height: '128px' }}
      />
    );
  }

  return (
    <div ref={containerRef} className="h-32 overflow-hidden bg-background border-b border-border relative">
      <canvas
        ref={canvasRef}
        onClick={handleClick}
        onMouseMove={handleMouseMove}
        onMouseLeave={handleMouseLeave}
        className="cursor-pointer block"
      />
      {tooltip && (
        <div
          className="fixed z-50 px-2 py-1 text-xs bg-background-tertiary border border-border rounded shadow-lg pointer-events-none"
          style={{
            left: tooltip.x + 10,
            top: tooltip.y + 10,
          }}
        >
          <div className="font-medium text-foreground">{tooltip.symbol}</div>
          <div className="text-foreground-secondary">{tooltip.timestamp}</div>
          <div className={`font-mono ${
            tooltip.value > 0 ? 'text-red-400' :
            tooltip.value < 0 ? 'text-blue-400' : 'text-foreground-muted'
          }`}>
            {isNaN(tooltip.value) ? 'N/A' : tooltip.value.toFixed(4)}
          </div>
        </div>
      )}
    </div>
  );
}
