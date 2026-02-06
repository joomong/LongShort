'use client';

import { useRef, useEffect, useCallback, useState, useMemo } from 'react';
import { useAlphaStore } from '@/store/alphaStore';
import { useThemeStore } from '@/store/themeStore';
import { getThemeColors } from '@/lib/themeColors';
import { signalToColor } from '@/lib/colorScale';

const MIN_CELL_WIDTH = 1;
const MIN_CELL_HEIGHT = 2;

// Hoisted static empty-state JSX (rendering-hoist-jsx)
const EMPTY_HEATMAP = (
  <div
    className="w-full border-b border-border bg-chart-background"
    style={{ height: '128px' }}
  />
);

interface TooltipInfo {
  x: number;
  y: number;
  symbol: string;
  timestamp: string;
  value: number | null;
}

export default function SignalHeatmap() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [tooltip, setTooltip] = useState<TooltipInfo | null>(null);
  // Store computed cell sizes so mouse handlers can use them
  const cellSizeRef = useRef({ w: MIN_CELL_WIDTH, h: MIN_CELL_HEIGHT });
  // Granular selectors — avoid subscribing to the full result object for selection state
  const heatmap = useAlphaStore(s => s.result?.heatmap);
  const selectedSymbol = useAlphaStore(s => s.selectedSymbol);
  const selectedTimestamp = useAlphaStore(s => s.selectedTimestamp);
  const setSelectedCell = useAlphaStore(s => s.setSelectedCell);
  const { theme } = useThemeStore();

  // Refs to avoid stale closures in stable draw callback
  const heatmapRef = useRef(heatmap);
  heatmapRef.current = heatmap;
  const selectedSymbolRef = useRef(selectedSymbol);
  selectedSymbolRef.current = selectedSymbol;
  const selectedTimestampRef = useRef(selectedTimestamp);
  selectedTimestampRef.current = selectedTimestamp;
  const themeRef = useRef(theme);
  themeRef.current = theme;

  // Build index Maps for O(1) lookup (js-index-maps)
  const symbolMap = useMemo(() => {
    if (!heatmap) return new Map<string, number>();
    return new Map(heatmap.symbols.map((s, i) => [s, i]));
  }, [heatmap]);

  const timeMap = useMemo(() => {
    if (!heatmap) return new Map<string, number>();
    return new Map(heatmap.timestamps.map((t, i) => [t, i]));
  }, [heatmap]);

  const symbolMapRef = useRef(symbolMap);
  symbolMapRef.current = symbolMap;
  const timeMapRef = useRef(timeMap);
  timeMapRef.current = timeMap;

   // Stable draw function that reads from refs to prevent infinite loops
   const drawHeatmap = useCallback(() => {
     const canvas = canvasRef.current;
     const container = containerRef.current;
     const currentHeatmap = heatmapRef.current;
     const currentSelectedSymbol = selectedSymbolRef.current;
     const currentSelectedTimestamp = selectedTimestampRef.current;
     const currentTheme = themeRef.current;
     const currentSymbolMap = symbolMapRef.current;
     const currentTimeMap = timeMapRef.current;

     if (!canvas || !container || !currentHeatmap) return;

     const ctx = canvas.getContext('2d');
     if (!ctx) return;

     void currentTheme; // Trigger redraw on theme change (getThemeColors reads CSS variables)
     const colors = getThemeColors();
     const { timestamps, symbols, values } = currentHeatmap;
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
     // Per-timestamp (column) rank-based normalization for cross-sectional color mapping
     const colSorted: number[][] = new Array(timestamps.length);
     for (let t = 0; t < timestamps.length; t++) {
       const col = values[t];
       const finite: number[] = [];
       if (col) {
         for (let s = 0; s < symbols.length; s++) {
           const v = col[s];
           if (typeof v === 'number' && isFinite(v)) finite.push(v);
         }
       }
       finite.sort((a, b) => a - b);
       colSorted[t] = finite;
     }

     symbols.forEach((_, symbolIdx) => {
       timestamps.forEach((_, timeIdx) => {
         const value = values[timeIdx]?.[symbolIdx];
         let color: string;
         if (typeof value !== 'number' || !isFinite(value)) {
           color = signalToColor(NaN, -1, 1);
         } else {
           const sorted = colSorted[timeIdx];
           if (sorted.length <= 1) {
             color = signalToColor(0, -1, 1);
           } else {
             // Binary search for rank
             let lo = 0, hi = sorted.length;
             while (lo < hi) {
               const mid = (lo + hi) >> 1;
               if (sorted[mid] < value) lo = mid + 1;
               else hi = mid;
             }
             const rank01 = lo / (sorted.length - 1); // 0 ~ 1
             const normalized = rank01 * 2 - 1;       // -1 ~ +1
             color = signalToColor(normalized, -1, 1);
           }
         }

         const x = Math.floor(timeIdx * cellW);
         const y = Math.floor(symbolIdx * cellH);
         const w = Math.ceil(cellW);
         const h = Math.ceil(cellH);

         ctx.fillStyle = color;
         ctx.fillRect(x, y, w, h);
       });
     });

     // Draw selection highlight — O(1) Map lookup instead of indexOf
     if (currentSelectedSymbol && currentSelectedTimestamp) {
       const symbolIdx = currentSymbolMap.get(currentSelectedSymbol);
       const timeIdx = currentTimeMap.get(currentSelectedTimestamp);
       if (symbolIdx !== undefined && timeIdx !== undefined) {
         const x = Math.floor(timeIdx * cellW);
         const y = Math.floor(symbolIdx * cellH);
         ctx.strokeStyle = colors.accent;
         ctx.lineWidth = 2;
         ctx.strokeRect(x, y, Math.ceil(cellW), Math.ceil(cellH));
       }
     }
   }, []); // Empty dependency array - all values accessed via refs

   // Redraw when data, selection, or theme changes
   useEffect(() => {
     drawHeatmap();
   }, [drawHeatmap, heatmap, selectedSymbol, selectedTimestamp, theme]);

  // Redraw on container resize
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const observer = new ResizeObserver(() => drawHeatmap());
    observer.observe(container);
    return () => observer.disconnect();
  }, [drawHeatmap]);

  const getCell = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current;
    if (!canvas || !heatmap) return null;

    const rect = canvas.getBoundingClientRect();
    // Account for CSS scaling (canvas pixel size vs display size)
    const scaleX = canvas.width / rect.width;
    const scaleY = canvas.height / rect.height;
    const x = (e.clientX - rect.left) * scaleX;
    const y = (e.clientY - rect.top) * scaleY;

    const { w, h } = cellSizeRef.current;
    const timeIdx = Math.floor(x / w);
    const symbolIdx = Math.floor(y / h);

    if (timeIdx >= 0 && timeIdx < heatmap.timestamps.length &&
        symbolIdx >= 0 && symbolIdx < heatmap.symbols.length) {
      return { timeIdx, symbolIdx };
    }
    return null;
  }, [heatmap]);

  const handleMouseMove = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const cell = getCell(e);
    if (!cell || !heatmap) { setTooltip(null); return; }

    const value = heatmap.values[cell.timeIdx]?.[cell.symbolIdx];
    setTooltip({
      x: e.clientX,
      y: e.clientY,
      symbol: heatmap.symbols[cell.symbolIdx],
      timestamp: heatmap.timestamps[cell.timeIdx],
      value: typeof value === 'number' ? value : null,
    });
  }, [heatmap, getCell]);

  const handleMouseLeave = useCallback(() => {
    setTooltip(null);
  }, []);

  const handleClick = useCallback((e: React.MouseEvent<HTMLCanvasElement>) => {
    const cell = getCell(e);
    if (!cell || !heatmap) return;

    const symbol = heatmap.symbols[cell.symbolIdx];
    const timestamp = heatmap.timestamps[cell.timeIdx];

    if (symbol && timestamp) {
      if (symbol === selectedSymbol && timestamp === selectedTimestamp) {
        setSelectedCell(null, null);
      } else {
        setSelectedCell(symbol, timestamp);
      }
    }
  }, [heatmap, selectedSymbol, selectedTimestamp, setSelectedCell, getCell]);

  if (!heatmap) {
    return EMPTY_HEATMAP;
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
          className="fixed z-50 px-3 py-2 text-xs bg-background-elevated border border-border-strong shadow-elevated pointer-events-none"
          style={{
            left: tooltip.x + 10,
            top: tooltip.y + 10,
          }}
        >
          <div className="font-medium text-foreground">{tooltip.symbol}</div>
          <div className="text-foreground-secondary">{tooltip.timestamp}</div>
          <div className={`font-mono ${
            tooltip.value === null ? 'text-foreground-muted' :
            tooltip.value > 0 ? 'text-metric-negative' :
            tooltip.value < 0 ? 'text-accent' : 'text-foreground-muted'
          }`}>
            {tooltip.value === null ? 'N/A' : tooltip.value.toFixed(4)}
          </div>
        </div>
      )}
    </div>
  );
}
