'use client';

import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useRunStore, selectSignalSource } from '@/runStore';
import { useThemeStore } from '@/theme';
import { useContainerSize, isMember } from '@/chart';
import { getThemeColors, parseHex, SIGNAL_HEATMAP_HEIGHT, OUT_OF_UNIVERSE_RGB, writeSignalPixel } from '@/chartStyle';
import { formatDate, symbolLabel, symbolMatches } from '@/format';

const MAX_CANVAS_DIM = 8192;

function SignalHeatmap() {
  const heatmap = useRunStore(state => selectSignalSource(state)?.plot);
  const active = useRunStore(state => state.viewMode !== 'detail' && state.result?.alphas.length === 1);
  const selectedSymbol = useRunStore(state => state.selectedSymbol);
  const selectedTimestamp = useRunStore(state => state.selectedTimestamp);
  const setSelectedCell = useRunStore(state => state.setSelectedCell);
  const labels = useRunStore(state => state.config?.symbolLabels);
  const theme = useThemeStore(state => state.theme);
  const [query, setQuery] = useState('');
  const [searchOpen, setSearchOpen] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const overlay = useRef<HTMLCanvasElement>(null);
  const searchContainer = useRef<HTMLDivElement>(null);
  const tooltip = useRef<HTMLDivElement>(null);
  const size = useContainerSize(container);
  const geometry = useRef({ cellW: 1, cellH: 1, width: 0, height: 0 });
  const previousRect = useRef<{ x: number; y: number; w: number; h: number } | null>(null);
  const latest = useRef({ heatmap, selectedSymbol, selectedTimestamp, labels, theme, active });
  latest.current = { heatmap, selectedSymbol, selectedTimestamp, labels, theme, active };
  const symbols = useMemo(() => new Map(heatmap?.symbols.map((value, index) => [value, index])), [heatmap?.symbols]);
  const dates = useMemo(() => new Map(heatmap?.timestamps.map((value, index) => [value, index])), [heatmap?.timestamps]);
  const indexes = useRef({ symbols, dates }); indexes.current = { symbols, dates };

  const drawOverlay = useCallback(() => {
    const ctx = overlay.current?.getContext('2d');
    if (!ctx || !latest.current.active) return;
    const old = previousRect.current;
    if (old) ctx.clearRect(old.x - 3, old.y - 3, old.w + 6, old.h + 6);
    previousRect.current = null;
    const { selectedSymbol: symbol, selectedTimestamp: timestamp } = latest.current;
    if (!symbol || timestamp === null) return;
    const column = indexes.current.symbols.get(symbol), row = indexes.current.dates.get(timestamp);
    if (column === undefined || row === undefined) return;
    const { cellW, cellH } = geometry.current;
    const rect = { x: Math.floor(row * cellW) - 1, y: Math.floor(column * cellH) - 1, w: Math.ceil(cellW) + 2, h: Math.ceil(cellH) + 2 };
    ctx.strokeStyle = getThemeColors(latest.current.theme).accent; ctx.lineWidth = 1;
    ctx.strokeRect(rect.x, rect.y, rect.w, rect.h); previousRect.current = rect;
  }, []);

  useEffect(() => {
    if (!active || !heatmap || !canvas.current || !overlay.current || size.width <= 0 || size.height <= 0) return;
    const ctx = canvas.current.getContext('2d'); if (!ctx) return;
    const { timestamps, symbols: names, values, membership, centers, scales } = heatmap;
    if (!timestamps.length || !names.length) return;
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const width = Math.max(1, Math.min(MAX_CANVAS_DIM, Math.round(size.width * dpr)));
    const height = Math.max(1, Math.min(MAX_CANVAS_DIM, Math.round(size.height * dpr)));
    for (const target of [canvas.current, overlay.current]) {
      target.width = width; target.height = height; target.style.width = `${size.width}px`; target.style.height = `${size.height}px`;
    }
    const cellW = width / timestamps.length, cellH = height / names.length;
    geometry.current = { width, height, cellW, cellH }; previousRect.current = null;
    const colors = getThemeColors(theme), base = parseHex(colors.chartBackground) ?? [255, 255, 255];
    const image = ctx.createImageData(width, height), pixels = image.data;
    const timeAtX = new Int32Array(width);
    for (let x = 0; x < width; ++x) timeAtX[x] = Math.min(timestamps.length - 1, Math.floor(x / cellW));
    for (let y = 0; y < height; ++y) {
      const symbol = Math.min(names.length - 1, Math.floor(y / cellH));
      for (let x = 0, offset = y * width * 4; x < width; ++x, offset += 4) {
        const time = timeAtX[x], value = values[time]?.[symbol], center = centers[time], scale = scales[time];
        if (!isMember(membership, time, symbol)) {
          pixels[offset] = OUT_OF_UNIVERSE_RGB[0]; pixels[offset + 1] = OUT_OF_UNIVERSE_RGB[1]; pixels[offset + 2] = OUT_OF_UNIVERSE_RGB[2];
        } else if (typeof value !== 'number' || !Number.isFinite(value) || center === null || scale === null) {
          pixels[offset] = base[0]; pixels[offset + 1] = base[1]; pixels[offset + 2] = base[2];
        } else {
          const z = scale > 0 ? Math.fround((value - center) / scale) : 0;
          writeSignalPixel(pixels, offset, z, base);
        }
        pixels[offset + 3] = 255;
      }
    }
    ctx.putImageData(image, 0, 0);
    drawOverlay();
  }, [active, heatmap, theme, size.width, size.height, drawOverlay]);
  useEffect(drawOverlay, [drawOverlay, selectedSymbol, selectedTimestamp, active]);
  useEffect(() => {
    if (!searchOpen) return;
    const close = (event: MouseEvent) => { if (!searchContainer.current?.contains(event.target as Node)) setSearchOpen(false); };
    document.addEventListener('mousedown', close, { passive: true });
    return () => document.removeEventListener('mousedown', close);
  }, [searchOpen]);
  useEffect(() => { if (!active && tooltip.current) tooltip.current.style.display = 'none'; }, [active]);

  const getCell = useCallback((event: React.MouseEvent<HTMLCanvasElement>) => {
    const current = latest.current.heatmap, element = canvas.current;
    if (!current || !element) return null;
    const rect = element.getBoundingClientRect();
    const row = Math.floor((event.clientX - rect.left) * element.width / rect.width / geometry.current.cellW);
    const column = Math.floor((event.clientY - rect.top) * element.height / rect.height / geometry.current.cellH);
    return row >= 0 && row < current.timestamps.length && column >= 0 && column < current.symbols.length ? { row, column } : null;
  }, []);
  const move = useCallback((event: React.MouseEvent<HTMLCanvasElement>) => {
    const cell = getCell(event), current = latest.current.heatmap, el = tooltip.current;
    if (!cell || !current || !el) { if (el) el.style.display = 'none'; return; }
    const value = current.values[cell.row]?.[cell.column], children = el.children as HTMLCollectionOf<HTMLElement>;
    children[0].textContent = symbolLabel(current.symbols[cell.column], latest.current.labels);
    children[1].textContent = formatDate(current.timestamps[cell.row]);
    children[2].textContent = typeof value === 'number' && Number.isFinite(value) ? value.toFixed(4) : 'N/A';
    el.style.left = `${event.clientX + 10}px`; el.style.top = `${event.clientY + 10}px`;
    el.style.transform = event.clientX > window.innerWidth * 0.6 ? 'translateX(calc(-100% - 20px))' : '';
    el.style.display = 'block';
  }, [getCell]);
  const click = useCallback((event: React.MouseEvent<HTMLCanvasElement>) => {
    const cell = getCell(event), current = latest.current;
    if (!cell || !current.heatmap) return;
    const symbol = current.heatmap.symbols[cell.column], timestamp = current.heatmap.timestamps[cell.row];
    if (symbol === current.selectedSymbol && timestamp === current.selectedTimestamp) setSelectedCell(null, null);
    else setSelectedCell(symbol, timestamp);
  }, [getCell, setSelectedCell]);
  const matches = query && heatmap ? heatmap.symbols.filter(symbol => symbolMatches(symbol, query, labels)).slice(0, 200) : [];
  if (!heatmap) return <div className="w-full shrink-0 border-b border-border bg-chart-background" style={{ height: SIGNAL_HEATMAP_HEIGHT }} />;
  return <div ref={container} style={{ height: SIGNAL_HEATMAP_HEIGHT }} className="shrink-0 min-h-0 overflow-hidden bg-background border-b border-border relative" data-testid="heatmap-panel">
    <div ref={searchContainer} className="absolute top-2 left-2 z-10">
      <input type="text" value={query} onChange={event => { setQuery(event.target.value); setSearchOpen(event.target.value.length > 0); }} onFocus={() => setSearchOpen(query.length > 0)} placeholder="Search symbol" className="w-28 px-2 py-0.5 text-[11px] font-mono bg-background border border-border text-foreground focus:outline-none focus:border-accent" />
      {searchOpen && matches.length ? <div className="absolute top-full left-0 mt-px w-28 max-h-[136px] overflow-y-auto bg-background border border-border shadow-card">
        {matches.map(symbol => <div key={symbol} data-symbol={symbol} className="px-2 py-0.5 text-[11px] font-mono text-foreground cursor-pointer hover:bg-row-active" onClick={() => { setSelectedCell(symbol, null); setQuery(''); setSearchOpen(false); }}>{symbolLabel(symbol, labels)}</div>)}
      </div> : null}
    </div>
    <canvas aria-label="Signal heatmap" ref={canvas} onClick={click} onMouseMove={move} onMouseLeave={() => { if (tooltip.current) tooltip.current.style.display = 'none'; }} className="cursor-pointer block" />
    <canvas aria-hidden="true" data-testid="heatmap-selection" ref={overlay} className="absolute inset-0 pointer-events-none" />
    <div ref={tooltip} role="tooltip" className="fixed z-50 px-2 py-1 text-[11px] text-[#222222] dark:text-[#dddddd] bg-[#f7f7f7] dark:bg-[#252525] border border-[#bdbdbd] dark:border-[#555555] shadow-sm pointer-events-none" style={{ display: 'none' }}>
      <div className="font-medium" /><div className="text-[#555555] dark:text-[#bbbbbb]" /><div className="font-mono" />
    </div>
  </div>;
}

export default memo(SignalHeatmap);
